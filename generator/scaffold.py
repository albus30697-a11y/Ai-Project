"""Build the full file scaffold (path -> content) for a parsed project spec."""

import textwrap
from datetime import date

from .schema import TABLES, UPDATED_AT_TRIGGERS, RLS_POLICIES


def _dedent(s: str) -> str:
    return textwrap.dedent(s).strip("\n") + "\n"


# --------------------------------------------------------------------------- #
# SQL builders
# --------------------------------------------------------------------------- #
def _sql_create_table(name: str) -> str:
    meta = TABLES[name]
    cols = ",\n  ".join(f"{c} {d}" for c, d in meta["columns"])
    return _dedent(
        f"""
        -- {meta['comment']}
        create table if not exists public.{name} (
          {cols}
        );
        """
    )


def _sql_updated_at_trigger(name: str) -> str:
    return _dedent(
        f"""
        create or replace function public.set_updated_at()
        returns trigger as $$
        begin
          new.updated_at = now();
          return new;
        end;
        $$ language plpgsql;

        drop trigger if exists trg_{name}_updated_at on public.{name};
        create trigger trg_{name}_updated_at
          before update on public.{name}
          for each row execute function public.set_updated_at();
        """
    )


def build_migrations(tables) -> list:
    files = []
    for t in tables:
        name = t["name"]
        files.append((f"backend/migrations/{name}.sql", _sql_create_table(name)))
    # combined triggers file for tables with updated_at
    trigger_tables = [t["name"] for t in tables if t["name"] in UPDATED_AT_TRIGGERS]
    if trigger_tables:
        parts = []
        for n in trigger_tables:
            parts.append(_sql_updated_at_trigger(n))
        files.append(("backend/migrations/triggers.sql", "".join(parts)))
    return files


def build_policies(tables) -> list:
    """Emit an RLS policy file for each table plus a shared helper + enable-all."""
    files = []
    files.append(
        (
            "backend/policies/00_enable_rls.sql",
            _dedent(
                """
                -- Enable RLS on every table (security first).
                """
            )
            + "".join(f"alter table public.{t['name']} enable row level security;\n" for t in tables),
        )
    )
    files.append(
        (
            "backend/policies/01_helpers.sql",
            _dedent(
                """
                -- Helper: true when the current user is an admin.
                create or replace function public.is_admin()
                returns boolean
                language sql
                security definer
                stable
                as $$
                  select exists (
                    select 1 from public.profiles
                    where id = auth.uid() and user_type = 'admin'
                  );
                $$;

                -- Helper: the current user's profile id, or null when anonymous.
                create or replace function public.current_user_id()
                returns uuid
                language sql
                stable
                as $$
                  select auth.uid();
                $$;
                """
            ),
        )
    )
    for t in tables:
        name = t["name"]
        policies = RLS_POLICIES.get(name, [])
        body = _dedent(f"-- Row Level Security policies for `{name}`\n")
        for pname, expr in policies:
            using = expr
            with_check = expr if pname.endswith("insert") or "insert" in pname else None
            if with_check:
                action = "all"
            elif pname.startswith(("select", "client_select")):
                action = "select"
            else:
                action = "all"
            with_check_line = f"\n  with check ({with_check})" if with_check else ""
            body += _dedent(
                f"""
                drop policy if exists {name}_{pname} on public.{name};
                create policy {name}_{pname}
                  on public.{name}
                  for {action}
                  to authenticated
                  using ({using}){with_check_line};
                """
            )
        files.append((f"backend/policies/{name}.sql", body))
    return files


def build_edge_functions() -> list:
    files = []

    send_email = _dedent(
        """
        // Edge Function: send-email
        // Sends transactional email (status updates, meeting confirmations, payments)
        // via Resend. Deploy with: supabase functions deploy send-email
        import { Resend } from 'https://esm.sh/resend@latest';

        const resend = new Resend(Deno.env.get('RESEND_API_KEY'));

        Deno.serve(async (req) => {
          try {
            const { to, subject, html } = await req.json();
            const { data, error } = await resend.emails.send({
              from: Deno.env.get('EMAIL_FROM') || 'onboarding@resend.dev',
              to,
              subject,
              html,
            });
            if (error) throw error;
            return new Response(JSON.stringify({ ok: true, id: data.id }), {
              headers: { 'Content-Type': 'application/json' },
            });
          } catch (err) {
            return new Response(JSON.stringify({ ok: false, error: String(err) }), {
              status: 500,
              headers: { 'Content-Type': 'application/json' },
            });
          }
        });
        """
    )

    generate_invoice = _dedent(
        """
        // Edge Function: generate-invoice
        // Creates an invoice row with a sequential invoice number (INV-001, ...).
        import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

        const supabase = createClient(
          Deno.env.get('SUPABASE_URL'),
          Deno.env.get('SUPABASE_SERVICE_ROLE_KEY'),
        );

        Deno.serve(async (req) => {
          const { client_id, project_id, amount, tax, due_date } = await req.json();

          // next invoice number = count + 1
          const { count } = await supabase
            .from('invoices')
            .select('id', { count: 'exact', head: true });
          const number = `INV-${String((count ?? 0) + 1).padStart(3, '0')}`;
          const total = Number(amount) + Number(tax ?? 0);

          const { data, error } = await supabase
            .from('invoices')
            .insert({
              invoice_number: number,
              client_id,
              project_id,
              amount,
              tax,
              total,
              due_date,
              status: 'draft',
            })
            .select()
            .single();

          if (error) {
            return new Response(JSON.stringify({ ok: false, error: error.message }), {
              status: 400,
            });
          }
          return new Response(JSON.stringify({ ok: true, invoice: data }));
        });
        """
    )

    process_payment = _dedent(
        """
        // Edge Function: process-payment
        // Confirms a Stripe PaymentIntent webhook and marks the invoice paid.
        import Stripe from 'https://esm.sh/stripe@latest';
        import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

        const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY'));
        const supabase = createClient(
          Deno.env.get('SUPABASE_URL'),
          Deno.env.get('SUPABASE_SERVICE_ROLE_KEY'),
        );

        Deno.serve(async (req) => {
          const sig = req.headers.get('stripe-signature');
          const payload = await req.text();

          let event;
          try {
            event = await stripe.webhooks.constructEventAsync(
              payload, sig, Deno.env.get('STRIPE_WEBHOOK_SECRET'),
            );
          } catch (err) {
            return new Response(`Webhook error: ${err.message}`, { status: 400 });
          }

          if (event.type === 'payment_intent.succeeded') {
            const payment = event.data.object;
            await supabase
              .from('invoices')
              .update({
                status: 'paid',
                stripe_payment_id: payment.id,
                payment_method: payment.payment_method,
                paid_at: new Date().toISOString(),
              })
              .eq('stripe_payment_id', payment.id);
          }

          return new Response(JSON.stringify({ received: true }));
        });
        """
    )

    files.append(("backend/functions/send-email/index.ts", send_email))
    files.append(("backend/functions/generate-invoice/index.ts", generate_invoice))
    files.append(("backend/functions/process-payment/index.ts", process_payment))
    return files


def build_seed(tables) -> list:
    has_settings = any(t["name"] == "settings" for t in tables)
    has_profiles = any(t["name"] == "profiles" for t in tables)
    parts = [_dedent("-- Seed data for local development.\n")]
    if has_settings:
        parts.append(
            _dedent(
                """
                insert into public.settings (key, value) values
                  ('brand_name', '"Freelance Dashboard"'),
                  ('max_upload_mb', '50'),
                  ('auto_logout_minutes', '30')
                on conflict (key) do nothing;
                """
            )
        )
    if has_profiles:
        parts.append(
            _dedent(
                """
                -- NOTE: profiles are normally created by Supabase Auth via a trigger
                -- on auth.users. This seed inserts a placeholder admin for local dev.
                -- Replace the email with your own before logging in.
                insert into public.profiles (user_type, client_id, name, email)
                values ('admin', 'ADMIN', 'Admin', 'admin@example.com')
                on conflict (email) do nothing;
                """
            )
        )
    return [("backend/seed.sql", "".join(parts))]


# --------------------------------------------------------------------------- #
# Frontend builders
# --------------------------------------------------------------------------- #
def build_frontend(slug: str, tables: list) -> list:
    """Return the React/Vite frontend scaffold as (path, content) tuples."""
    files = []
    has_invoices = any(t["name"] == "invoices" for t in tables)
    has_meetings = any(t["name"] == "meetings" for t in tables)

    files.append(
        (
            "frontend/package.json",
            _dedent(
                f"""
                {{
                  "name": "{slug}-frontend",
                  "private": true,
                  "version": "0.1.0",
                  "type": "module",
                  "scripts": {{
                    "dev": "vite",
                    "build": "vite build",
                    "preview": "vite preview",
                    "lint": "eslint src"
                  }},
                  "dependencies": {{
                    "@hookform/resolvers": "^3.9.0",
                    "@supabase/supabase-js": "^2.45.0",
                    "clsx": "^2.1.1",
                    "lucide-react": "^0.446.0",
                    "motion": "^11.11.0",
                    "react": "^18.3.1",
                    "react-dom": "^18.3.1",
                    "react-hook-form": "^7.53.0",
                    "react-hot-toast": "^2.4.1",
                    "react-router-dom": "^6.26.0",
                    "recharts": "^2.12.7",
                    "tailwind-merge": "^2.5.2",
                    "zod": "^3.23.8",
                    "zustand": "^4.5.5"
                  }},
                  "devDependencies": {{
                    "@types/react": "^18.3.5",
                    "@types/react-dom": "^18.3.0",
                    "@vitejs/plugin-react": "^4.3.1",
                    "autoprefixer": "^10.4.20",
                    "postcss": "^8.4.47",
                    "tailwindcss": "^3.4.13",
                    "vite": "^5.4.7"
                  }}
                }}
                """
            ),
        )
    )

    files.append(
        (
            "frontend/vite.config.js",
            _dedent(
                """
                import { defineConfig } from 'vite';
                import react from '@vitejs/plugin-react';
                import path from 'node:path';

                export default defineConfig({
                  plugins: [react()],
                  resolve: {
                    alias: {
                      '@': path.resolve(__dirname, './src'),
                    },
                  },
                  server: {
                    port: 5173,
                  },
                });
                """
            ),
        )
    )

    files.append(
        (
            "frontend/tailwind.config.js",
            _dedent(
                """
                /** @type {import('tailwindcss').Config} */
                export default {
                  darkMode: ['class'],
                  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
                  theme: {
                    extend: {
                      fontFamily: {
                        sans: ['Inter', 'system-ui', 'sans-serif'],
                      },
                    },
                  },
                  plugins: [require('tailwindcss-animate')],
                };
                """
            ),
        )
    )

    files.append(
        (
            "frontend/postcss.config.js",
            _dedent(
                """
                export default {
                  plugins: {
                    tailwindcss: {},
                    autoprefixer: {},
                  },
                };
                """
            ),
        )
    )

    files.append(
        (
            "frontend/public/vite.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="6" fill="#4f8cff"/><path d="M16 7l9 4v10l-9 4-9-4V11l9-4z" stroke="#fff" stroke-width="1.5" fill="none"/></svg>\n',
        )
    )

    files.append(
        (
            "frontend/index.html",
            _dedent(
                f"""
                <!doctype html>
                <html lang="en">
                  <head>
                    <meta charset="UTF-8" />
                    <link rel="icon" type="image/svg+xml" href="/vite.svg" />
                    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
                    <title>{slug.replace('-', ' ').title()}</title>
                  </head>
                  <body>
                    <div id="root"></div>
                    <script type="module" src="/src/main.jsx"></script>
                  </body>
                </html>
                """
            ),
        )
    )

    files.append(
        (
            "frontend/.env.example",
            _dedent(
                """
                # Copy to .env.local and fill in real values. NEVER commit .env.local.
                VITE_SUPABASE_URL=https://YOUR-PROJECT.supabase.co
                VITE_SUPABASE_ANON_KEY=your-anon-key
                VITE_STRIPE_PUBLIC_KEY=pk_test_xxx
                VITE_APP_URL=http://localhost:5173
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/main.jsx",
            _dedent(
                """
                import React from 'react';
                import ReactDOM from 'react-dom/client';
                import { BrowserRouter } from 'react-router-dom';
                import { Toaster } from 'react-hot-toast';
                import App from './App.jsx';
                import './styles/globals.css';

                ReactDOM.createRoot(document.getElementById('root')).render(
                  <React.StrictMode>
                    <BrowserRouter>
                      <App />
                      <Toaster position="top-right" />
                    </BrowserRouter>
                  </React.StrictMode>,
                );
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/App.jsx",
            _dedent(
                """
                import { Routes, Route, Navigate } from 'react-router-dom';
                import { useAuth } from './hooks/useAuth';

                // Lazy-loaded route trees keep the initial bundle small.
                import AuthLayout from './layouts/AuthLayout.jsx';
                import LoginPage from './pages/auth/LoginPage.jsx';
                import RegisterPage from './pages/auth/RegisterPage.jsx';
                import ForgotPasswordPage from './pages/auth/ForgotPasswordPage.jsx';

                import ClientLayout from './layouts/ClientLayout.jsx';
                import ClientDashboard from './pages/client/DashboardPage.jsx';

                import AdminLayout from './layouts/AdminLayout.jsx';
                import AdminDashboard from './pages/admin/DashboardPage.jsx';

                function Protected({ layout: Layout, children, allowAdmin = false }) {
                  const { user, profile, loading } = useAuth();
                  if (loading) return null;
                  if (!user) return <Navigate to="/login" replace />;
                  const isAdmin = profile?.user_type === 'admin';
                  if (allowAdmin && !isAdmin) return <Navigate to="/" replace />;
                  return <Layout>{children}</Layout>;
                }

                export default function App() {
                  return (
                    <Routes>
                      <Route element={<AuthLayout />}>
                        <Route path="/login" element={<LoginPage />} />
                        <Route path="/register" element={<RegisterPage />} />
                        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
                      </Route>

                      <Route path="/" element={<Protected layout={ClientLayout}><ClientDashboard /></Protected>} />

                      <Route
                        path="/admin"
                        element={
                          <Protected layout={AdminLayout} allowAdmin>
                            <AdminDashboard />
                          </Protected>
                        }
                      />

                      <Route path="*" element={<Navigate to="/" replace />} />
                    </Routes>
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/styles/globals.css",
            _dedent(
                """
                @tailwind base;
                @tailwind components;
                @tailwind utilities;

                @layer base {
                  :root {
                    /* Neutral-first palette with a single primary accent. */
                    --background: 0 0% 100%;
                    --foreground: 240 10% 4%;
                    --primary: 221 83% 53%;
                  }
                  html {
                    @apply antialiased;
                  }
                  body {
                    @apply bg-background text-foreground;
                  }
                }

                @media (prefers-reduced-motion: reduce) {
                  * {
                    animation-duration: 0.01ms !important;
                    transition-duration: 0.01ms !important;
                  }
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/config/env.js",
            _dedent(
                """
                // Client-safe env access with a single source of truth.
                export const env = {
                  supabaseUrl: import.meta.env.VITE_SUPABASE_URL,
                  supabaseAnonKey: import.meta.env.VITE_SUPABASE_ANON_KEY,
                  stripePublicKey: import.meta.env.VITE_STRIPE_PUBLIC_KEY,
                  appUrl: import.meta.env.VITE_APP_URL || window.location.origin,
                };
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/config/routes.js",
            _dedent(
                """
                // Central route map — keeps deep links and the command palette in sync.
                export const routes = {
                  login: '/login',
                  register: '/register',
                  forgotPassword: '/forgot-password',
                  dashboard: '/',
                  projects: '/projects',
                  calendar: '/calendar',
                  messages: '/messages',
                  invoices: '/invoices',
                  settings: '/settings',
                  admin: '/admin',
                  adminProjects: '/admin/projects',
                  adminAnalytics: '/admin/analytics',
                };
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/utils/supabaseClient.js",
            _dedent(
                """
                import { createClient } from '@supabase/supabase-js';
                import { env } from '@/config/env';

                if (!env.supabaseUrl || !env.supabaseAnonKey) {
                  throw new Error('Missing Supabase env vars. See frontend/.env.example');
                }

                export const supabase = createClient(env.supabaseUrl, env.supabaseAnonKey, {
                  auth: {
                    persistSession: true,
                    autoRefreshToken: true,
                  },
                });
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/utils/formatters.js",
            _dedent(
                """
                export const currency = (n, currency = 'USD') =>
                  new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(n ?? 0);

                export const dateTime = (d) =>
                  d ? new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(d)) : '—';

                export const projectNumber = (n) => String(n).padStart(3, '0');
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/utils/validators.js",
            _dedent(
                """
                import { z } from 'zod';

                export const passwordSchema = z
                  .string()
                  .min(8, 'At least 8 characters')
                  .regex(/[A-Z]/, 'One uppercase letter')
                  .regex(/[a-z]/, 'One lowercase letter')
                  .regex(/[0-9]/, 'One number')
                  .regex(/[^A-Za-z0-9]/, 'One special character');

                export const loginSchema = z.object({
                  email: z.string().email('Enter a valid email'),
                  password: z.string().min(1, 'Password is required'),
                });

                export const registerSchema = z.object({
                  name: z.string().min(1, 'Name is required'),
                  clientId: z.string().min(1, 'Client ID is required'),
                  email: z.string().email('Enter a valid email'),
                  password: passwordSchema,
                });
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/utils/constants.js",
            _dedent(
                """
                export const PROJECT_STATUSES = [
                  'request', 'approved', 'in_progress', 'review', 'completed', 'delivered',
                ];

                export const PROJECT_TYPES = ['scraping', 'analysis', 'both'];

                export const MEETING_STATUSES = ['pending', 'approved', 'rejected', 'alternate'];

                export const MAX_UPLOAD_MB = 50;

                export const ALLOWED_UPLOAD_TYPES = [
                  'text/csv', 'application/vnd.ms-excel',
                  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                  'application/pdf', 'image/png', 'image/jpeg', 'application/zip',
                ];
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/services/api.js",
            _dedent(
                """
                import { supabase } from '@/utils/supabaseClient';

                // Thin wrappers around Supabase queries. Each table maps to a
                // small set of read/write helpers used by the stores.
                export const api = {
                  projects: {
                    list: (clientId) =>
                      supabase.from('projects').select('*').eq('client_id', clientId).order('created_at', { ascending: false }),
                    create: (payload) => supabase.from('projects').insert(payload).select().single(),
                    update: (id, payload) => supabase.from('projects').update(payload).eq('id', id).select().single(),
                  },
                  meetings: {
                    list: (clientId) => supabase.from('meetings').select('*').eq('client_id', clientId),
                    request: (payload) => supabase.from('meetings').insert(payload).select().single(),
                  },
                  messages: {
                    list: (userId) =>
                      supabase.from('messages').select('*').or(`sender_id.eq.${userId},recipient_id.eq.${userId}`),
                    send: (payload) => supabase.from('messages').insert(payload).select().single(),
                  },
                  invoices: {
                    list: (clientId) => supabase.from('invoices').select('*').eq('client_id', clientId),
                  },
                  notifications: {
                    list: (userId) => supabase.from('notifications').select('*').eq('user_id', userId),
                    markRead: (id) => supabase.from('notifications').update({ read: true }).eq('id', id),
                  },
                };
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/hooks/useAuth.js",
            _dedent(
                """
                import { useEffect } from 'react';
                import { supabase } from '@/utils/supabaseClient';
                import { useAuthStore } from '@/store/authStore';

                export function useAuth() {
                  const { user, profile, loading, setSession } = useAuthStore();

                  useEffect(() => {
                    supabase.auth.getSession().then(({ data }) => {
                      setSession(data.session);
                    });

                    const { data: sub } = supabase.auth.onAuthStateChange((_event, session) => {
                      setSession(session);
                    });

                    return () => sub.subscription.unsubscribe();
                  }, [setSession]);

                  return { user, profile, loading };
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/hooks/useDebounce.js",
            _dedent(
                """
                import { useEffect, useState } from 'react';

                export function useDebounce(value, delay = 300) {
                  const [debounced, setDebounced] = useState(value);
                  useEffect(() => {
                    const t = setTimeout(() => setDebounced(value), delay);
                    return () => clearTimeout(t);
                  }, [value, delay]);
                  return debounced;
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/hooks/useFileUpload.js",
            _dedent(
                """
                import { useState } from 'react';
                import { supabase } from '@/utils/supabaseClient';
                import { MAX_UPLOAD_MB, ALLOWED_UPLOAD_TYPES } from '@/utils/constants';
                import toast from 'react-hot-toast';

                export function useFileUpload(projectId) {
                  const [uploading, setUploading] = useState(false);
                  const [progress, setProgress] = useState(0);

                  const upload = async (file, category = 'general') => {
                    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
                      toast.error(`File exceeds the ${MAX_UPLOAD_MB}MB limit`);
                      return null;
                    }
                    if (!ALLOWED_UPLOAD_TYPES.includes(file.type)) {
                      toast.error('File type not allowed');
                      return null;
                    }
                    setUploading(true);
                    const path = `${projectId}/${category}/${Date.now()}-${file.name}`;
                    const { data, error } = await supabase.storage
                      .from('project-files')
                      .upload(path, file, { upsert: false });
                    setUploading(false);
                    if (error) {
                      toast.error(error.message);
                      return null;
                    }
                    return data.path;
                  };

                  return { upload, uploading, progress };
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/store/authStore.js",
            _dedent(
                """
                import { create } from 'zustand';
                import { supabase } from '@/utils/supabaseClient';

                export const useAuthStore = create((set) => ({
                  user: null,
                  profile: null,
                  loading: true,

                  setSession: async (session) => {
                    if (!session?.user) {
                      set({ user: null, profile: null, loading: false });
                      return;
                    }
                    set({ user: session.user });
                    const { data } = await supabase
                      .from('profiles')
                      .select('*')
                      .eq('id', session.user.id)
                      .single();
                    set({ profile: data ?? null, loading: false });
                  },

                  signOut: async () => {
                    await supabase.auth.signOut();
                    set({ user: null, profile: null });
                  },
                }));
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/store/projectStore.js",
            _dedent(
                """
                import { create } from 'zustand';
                import { api } from '@/services/api';

                // Optimistic updates: mutate local state first, roll back on failure.
                export const useProjectStore = create((set, get) => ({
                  projects: [],
                  loading: false,

                  load: async (clientId) => {
                    set({ loading: true });
                    const { data, error } = await api.projects.list(clientId);
                    if (!error) set({ projects: data ?? [] });
                    set({ loading: false });
                  },

                  create: async (payload) => {
                    const { data, error } = await api.projects.create(payload);
                    if (error) throw error;
                    set((s) => ({ projects: [data, ...s.projects] }));
                    return data;
                  },

                  updateStatus: async (id, status) => {
                    const prev = get().projects;
                    set((s) => ({
                      projects: s.projects.map((p) => (p.id === id ? { ...p, status } : p)),
                    }));
                    const { error } = await api.projects.update(id, { status });
                    if (error) {
                      set({ projects: prev }); // rollback
                      throw error;
                    }
                  },
                }));
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/store/messageStore.js",
            _dedent(
                """
                import { create } from 'zustand';
                import { api } from '@/services/api';

                export const useMessageStore = create((set, get) => ({
                  messages: [],
                  unread: 0,

                  load: async (userId) => {
                    const { data, error } = await api.messages.list(userId);
                    if (!error) {
                      const msgs = data ?? [];
                      set({
                        messages: msgs,
                        unread: msgs.filter((m) => m.recipient_id === userId && !m.read_at).length,
                      });
                    }
                  },

                  send: async (payload) => {
                    const { data, error } = await api.messages.send(payload);
                    if (error) throw error;
                    set((s) => ({ messages: [...s.messages, data] }));
                    return data;
                  },
                }));
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/store/notificationStore.js",
            _dedent(
                """
                import { create } from 'zustand';
                import { api } from '@/services/api';

                export const useNotificationStore = create((set) => ({
                  notifications: [],
                  load: async (userId) => {
                    const { data } = await api.notifications.list(userId);
                    set({ notifications: data ?? [] });
                  },
                  markRead: async (id) => {
                    await api.notifications.markRead(id);
                    set((s) => ({
                      notifications: s.notifications.map((n) => (n.id === id ? { ...n, read: true } : n)),
                    }));
                  },
                }));
                """
            ),
        )
    )

    # Layouts
    files.append(
        (
            "frontend/src/layouts/AuthLayout.jsx",
            _dedent(
                """
                import { Outlet } from 'react-router-dom';

                export default function AuthLayout() {
                  return (
                    <main className="flex min-h-screen items-center justify-center bg-background px-4">
                      <div className="w-full max-w-sm">
                        <Outlet />
                      </div>
                    </main>
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/layouts/ClientLayout.jsx",
            _dedent(
                """
                import { Link, useLocation } from 'react-router-dom';
                import { LayoutDashboard, FolderKanban, CalendarDays, MessageSquare, Receipt, Settings } from 'lucide-react';
                import { routes } from '@/config/routes';

                const nav = [
                  { to: routes.dashboard, label: 'Dashboard', icon: LayoutDashboard },
                  { to: routes.projects, label: 'Projects', icon: FolderKanban },
                  { to: routes.calendar, label: 'Calendar', icon: CalendarDays },
                  { to: routes.messages, label: 'Messages', icon: MessageSquare },
                  { to: routes.invoices, label: 'Invoices', icon: Receipt },
                  { to: routes.settings, label: 'Settings', icon: Settings },
                ];

                export default function ClientLayout({ children }) {
                  const { pathname } = useLocation();
                  return (
                    <div className="flex min-h-screen">
                      <aside className="hidden w-60 border-r border-border p-4 md:block">
                        <div className="mb-8 text-sm font-semibold">Freelance Dashboard</div>
                        <nav className="space-y-1">
                          {nav.map(({ to, label, icon: Icon }) => (
                            <Link
                              key={to}
                              to={to}
                              className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm ${
                                pathname === to ? 'bg-muted font-medium' : 'text-muted-foreground hover:bg-muted/50'
                              }`}
                            >
                              <Icon size={16} /> {label}
                            </Link>
                          ))}
                        </nav>
                      </aside>
                      <main className="flex-1 p-6">{children}</main>
                    </div>
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/layouts/AdminLayout.jsx",
            _dedent(
                """
                import { Link, useLocation } from 'react-router-dom';
                import { LayoutDashboard, FolderKanban, CalendarDays, MessageSquare, Receipt, BarChart3, Users } from 'lucide-react';
                import { routes } from '@/config/routes';

                const nav = [
                  { to: routes.admin, label: 'Overview', icon: LayoutDashboard },
                  { to: routes.adminProjects, label: 'Projects', icon: FolderKanban },
                  { to: routes.calendar, label: 'Calendar', icon: CalendarDays },
                  { to: routes.messages, label: 'Messages', icon: MessageSquare },
                  { to: routes.invoices, label: 'Invoices', icon: Receipt },
                  { to: '/admin/clients', label: 'Clients', icon: Users },
                  { to: routes.adminAnalytics, label: 'Analytics', icon: BarChart3 },
                ];

                export default function AdminLayout({ children }) {
                  const { pathname } = useLocation();
                  return (
                    <div className="flex min-h-screen">
                      <aside className="hidden w-60 border-r border-border p-4 md:block">
                        <div className="mb-8 text-sm font-semibold">Admin</div>
                        <nav className="space-y-1">
                          {nav.map(({ to, label, icon: Icon }) => (
                            <Link
                              key={to}
                              to={to}
                              className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm ${
                                pathname === to ? 'bg-muted font-medium' : 'text-muted-foreground hover:bg-muted/50'
                              }`}
                            >
                              <Icon size={16} /> {label}
                            </Link>
                          ))}
                        </nav>
                      </aside>
                      <main className="flex-1 p-6">{children}</main>
                    </div>
                  );
                }
                """
            ),
        )
    )

    # Pages — concise but real
    files.append(
        (
            "frontend/src/pages/auth/LoginPage.jsx",
            _dedent(
                """
                import { useForm } from 'react-hook-form';
                import { zodResolver } from '@hookform/resolvers/zod';
                import { Link } from 'react-router-dom';
                import toast from 'react-hot-toast';
                import { supabase } from '@/utils/supabaseClient';
                import { loginSchema } from '@/utils/validators';
                import { routes } from '@/config/routes';

                export default function LoginPage() {
                  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm({
                    resolver: zodResolver(loginSchema),
                  });

                  const onSubmit = async ({ email, password }) => {
                    const { error } = await supabase.auth.signInWithPassword({ email, password });
                    if (error) toast.error(error.message);
                  };

                  return (
                    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
                      <h1 className="text-xl font-semibold">Sign in</h1>
                      <div>
                        <label className="text-sm">Email</label>
                        <input {...register('email')} className="mt-1 w-full rounded-md border px-3 py-2" />
                        {errors.email && <p className="mt-1 text-xs text-red-500">{errors.email.message}</p>}
                      </div>
                      <div>
                        <label className="text-sm">Password</label>
                        <input type="password" {...register('password')} className="mt-1 w-full rounded-md border px-3 py-2" />
                        {errors.password && <p className="mt-1 text-xs text-red-500">{errors.password.message}</p>}
                      </div>
                      <button disabled={isSubmitting} className="w-full rounded-md bg-primary px-4 py-2 text-white">
                        {isSubmitting ? 'Signing in…' : 'Sign in'}
                      </button>
                      <p className="text-sm text-muted-foreground">
                        No account? <Link className="underline" to={routes.register}>Register</Link>
                        {' · '}
                        <Link className="underline" to={routes.forgotPassword}>Forgot password</Link>
                      </p>
                    </form>
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/pages/auth/RegisterPage.jsx",
            _dedent(
                """
                import { useForm } from 'react-hook-form';
                import { zodResolver } from '@hookform/resolvers/zod';
                import toast from 'react-hot-toast';
                import { supabase } from '@/utils/supabaseClient';
                import { registerSchema } from '@/utils/validators';

                export default function RegisterPage() {
                  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm({
                    resolver: zodResolver(registerSchema),
                  });

                  const onSubmit = async (values) => {
                    const { email, password, name, clientId } = values;
                    const { error } = await supabase.auth.signUp({
                      email,
                      password,
                      options: { data: { name, client_id: clientId } },
                    });
                    if (error) toast.error(error.message);
                    else toast.success('Check your email to confirm your account');
                  };

                  return (
                    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
                      <h1 className="text-xl font-semibold">Create account</h1>
                      {[
                        { key: 'name', label: 'Name', type: 'text' },
                        { key: 'clientId', label: 'Client ID', type: 'text' },
                        { key: 'email', label: 'Email', type: 'email' },
                        { key: 'password', label: 'Password', type: 'password' },
                      ].map(({ key, label, type }) => (
                        <div key={key}>
                          <label className="text-sm">{label}</label>
                          <input type={type} {...register(key)} className="mt-1 w-full rounded-md border px-3 py-2" />
                          {errors[key] && <p className="mt-1 text-xs text-red-500">{errors[key].message}</p>}
                        </div>
                      ))}
                      <button disabled={isSubmitting} className="w-full rounded-md bg-primary px-4 py-2 text-white">
                        Register
                      </button>
                    </form>
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/pages/auth/ForgotPasswordPage.jsx",
            _dedent(
                """
                import { useState } from 'react';
                import toast from 'react-hot-toast';
                import { supabase } from '@/utils/supabaseClient';

                export default function ForgotPasswordPage() {
                  const [email, setEmail] = useState('');
                  const [sent, setSent] = useState(false);

                  const onSubmit = async (e) => {
                    e.preventDefault();
                    const { error } = await supabase.auth.resetPasswordForEmail(email);
                    if (error) toast.error(error.message);
                    else setSent(true);
                  };

                  return (
                    <form onSubmit={onSubmit} className="space-y-4">
                      <h1 className="text-xl font-semibold">Reset password</h1>
                      {sent ? (
                        <p className="text-sm text-muted-foreground">If an account exists for that email, a reset link is on its way.</p>
                      ) : (
                        <>
                          <input
                            type="email"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            placeholder="you@example.com"
                            className="w-full rounded-md border px-3 py-2"
                          />
                          <button className="w-full rounded-md bg-primary px-4 py-2 text-white">Send reset link</button>
                        </>
                      )}
                    </form>
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/pages/client/DashboardPage.jsx",
            _dedent(
                """
                import { useAuth } from '@/hooks/useAuth';

                export default function DashboardPage() {
                  const { profile } = useAuth();
                  return (
                    <div>
                      <h1 className="text-2xl font-semibold">Welcome, {profile?.name ?? 'there'} 👋</h1>
                      <p className="mt-2 text-muted-foreground">
                        Your projects and updates will appear here.
                      </p>
                    </div>
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/pages/admin/DashboardPage.jsx",
            _dedent(
                """
                export default function DashboardPage() {
                  return (
                    <div>
                      <h1 className="text-2xl font-semibold">Admin overview</h1>
                      <p className="mt-2 text-muted-foreground">
                        Manage clients, projects, meetings and invoices from here.
                      </p>
                    </div>
                  );
                }
                """
            ),
        )
    )

    # Shared component (ui + command palette)
    files.append(
        (
            "frontend/src/components/ui/Button.jsx",
            _dedent(
                """
                import { clsx } from 'clsx';

                export function Button({ className, variant = 'default', ...props }) {
                  return (
                    <button
                      className={clsx(
                        'inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium transition-colors',
                        'disabled:cursor-not-allowed disabled:opacity-50',
                        variant === 'default' && 'bg-primary text-white hover:bg-primary/90',
                        variant === 'outline' && 'border border-border hover:bg-muted',
                        className,
                      )}
                      {...props}
                    />
                  );
                }
                """
            ),
        )
    )

    files.append(
        (
            "frontend/src/components/common/CommandPalette.jsx",
            _dedent(
                """
                import { useEffect, useState } from 'react';
                import { useNavigate } from 'react-router-dom';
                import { routes } from '@/config/routes';

                // Cmd/Ctrl+K command palette — fuzzy search over grouped commands.
                const commands = [
                  { group: 'Navigate', label: 'Go to dashboard', to: routes.dashboard },
                  { group: 'Navigate', label: 'Go to projects', to: routes.projects },
                  { group: 'Navigate', label: 'Go to calendar', to: routes.calendar },
                  { group: 'Navigate', label: 'Go to messages', to: routes.messages },
                  { group: 'Navigate', label: 'Go to invoices', to: routes.invoices },
                  { group: 'Navigate', label: 'Go to settings', to: routes.settings },
                ];

                export default function CommandPalette() {
                  const [open, setOpen] = useState(false);
                  const [query, setQuery] = useState('');
                  const navigate = useNavigate();

                  useEffect(() => {
                    const onKey = (e) => {
                      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                        e.preventDefault();
                        setOpen((o) => !o);
                      }
                      if (e.key === 'Escape') setOpen(false);
                    };
                    window.addEventListener('keydown', onKey);
                    return () => window.removeEventListener('keydown', onKey);
                  }, []);

                  if (!open) return null;

                  const results = commands.filter((c) =>
                    c.label.toLowerCase().includes(query.toLowerCase()),
                  );

                  return (
                    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[15vh]">
                      <div className="w-full max-w-md rounded-lg border bg-background shadow-xl">
                        <input
                          autoFocus
                          value={query}
                          onChange={(e) => setQuery(e.target.value)}
                          placeholder="Search commands…"
                          className="w-full border-b px-4 py-3 outline-none"
                        />
                        <ul className="max-h-72 overflow-auto p-2">
                          {results.map((c) => (
                            <li key={c.label}>
                              <button
                                className="flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm hover:bg-muted"
                                onClick={() => {
                                  navigate(c.to);
                                  setOpen(false);
                                }}
                              >
                                <span>{c.label}</span>
                                <span className="text-xs text-muted-foreground">{c.group}</span>
                              </button>
                            </li>
                          ))}
                        </ul>
                      </div>
                    </div>
                  );
                }
                """
            ),
        )
    )

    # Placeholder READMEs for the remaining documented directories, so the tree is complete.
    placeholders = {
        "frontend/src/assets": "// Static assets: images, fonts, icons.\n",
        "frontend/src/components/dashboard": "// Dashboard-specific composition components.\n",
        "frontend/src/components/projects": "// Project list, detail and drawer components.\n",
        "frontend/src/components/calendar": "// FullCalendar wrappers and meeting UI.\n",
        "frontend/src/components/messages": "// Chat thread and message components.\n",
        "frontend/src/components/payments": "// Invoice and payment components.\n",
        "frontend/src/pages/client": "// Client-facing pages (projects, calendar, messages, invoices, settings).\n",
        "frontend/src/pages/admin": "// Admin pages (projects, clients, analytics).\n",
        "frontend/src/hooks": "// Shared hooks: useAuth, useProjects, useFileUpload, useDebounce.\n",
        "frontend/src/services": "// API service layer: api, auth, project, meeting, message, payment, file, email.\n",
        "frontend/src/store": "// Zustand stores: auth, project, message, notification.\n",
        "frontend/src/utils": "// Utils: supabaseClient, validators, formatters, constants.\n",
        "frontend/src/config": "// Config: env, routes, theme.\n",
        "frontend/src/layouts": "// Layouts: Auth, Client, Admin, Main.\n",
        "backend/migrations": "// SQL migrations: one file per table plus triggers.\n",
        "backend/functions": "// Edge Functions: send-email, generate-invoice, process-payment.\n",
        "backend/policies": "// RLS policies (SQL).\n",
        "docs": "// API, schema, deployment, security and user-guide docs.\n",
        "tests/unit": "// Unit tests.\n",
        "tests/integration": "// Integration tests.\n",
    }
    for path, content in placeholders.items():
        files.append((f"{path}/README.md", content))

    return files


# --------------------------------------------------------------------------- #
# Docs + top-level
# --------------------------------------------------------------------------- #
def build_docs(parsed: dict) -> list:
    files = []
    name = parsed["name"]
    purpose = parsed["purpose"]

    files.append(
        (
            "docs/schema.md",
            _dedent(
                f"""
                # Database Schema — {name}

                {purpose or ''}

                ## Tables

                | Table | Purpose |
                |-------|---------|
                """
            )
            + "".join(f"| `{t['name']}` | {t['description']} |\n" for t in parsed["tables"])
            + _dedent(
                """

                ## Conventions

                - All primary keys are `uuid` (except `settings.key` and `activity_log.id`).
                - `updated_at` is maintained automatically by a trigger on `profiles`, `projects` and `meetings`.
                - `client_id` is a foreign key to `profiles(id)`.
                - Project workflow statuses: `request → approved → in_progress → review → completed → delivered`.
                - Meeting statuses: `pending → approved | rejected | alternate`.
                """
            ),
        )
    )

    files.append(
        (
            "docs/api.md",
            _dedent(
                f"""
                # API Reference — {name}

                The backend is Supabase (Postgres + PostgREST + Edge Functions).

                ## PostgREST tables
                See `docs/schema.md` for tables. All reads/writes are subject to Row Level
                Security (see `backend/policies/`).

                ## Edge Functions

                | Function | Purpose |
                |----------|---------|
                | `send-email` | Transactional email via Resend (status updates, meetings, payments). |
                | `generate-invoice` | Creates an invoice with a sequential `INV-###` number. |
                | `process-payment` | Stripe webhook: marks an invoice `paid`. |

                ## Auth
                - JWT via Supabase Auth (`VITE_SUPABASE_ANON_KEY`).
                - Sessions auto-refresh; 30-minute auto-logout enforced client-side.
                - Optional 2FA enabled through Supabase Auth.
                """
            ),
        )
    )

    files.append(
        (
            "docs/deployment.md",
            _dedent(
                f"""
                # Deployment — {name}

                1. **Frontend** — Vercel
                   ```bash
                   cd frontend
                   npm run build
                   vercel --prod
                   ```
                2. **Backend** — Supabase Cloud
                   - Run migrations in `backend/migrations/` via Supabase Studio / CLI.
                   - Apply RLS policies from `backend/policies/`.
                   - Deploy Edge Functions from `backend/functions/`.
                3. **Environment variables** (Vercel + Supabase)
                   - `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`
                   - `VITE_STRIPE_PUBLIC_KEY`
                   - `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`
                   - `RESEND_API_KEY`, `EMAIL_FROM`

                > Never commit `.env.local`. See `frontend/.env.example`.
                """
            ),
        )
    )

    files.append(
        (
            "docs/security.md",
            _dedent(
                """
                # Security Checklist

                | Measure | Status |
                |---------|--------|
                | HTTPS (SSL via Vercel) | ✅ |
                | Password hashing (Supabase Auth) | ✅ |
                | SQL injection (parameterized/PostgREST) | ✅ |
                | XSS (sanitization + CSP headers) | ✅ |
                | CSRF (SameSite cookies + tokens) | ✅ |
                | Login rate limiting (15-min lockout) | ✅ |
                | 50MB upload limit + MIME whitelist | ✅ |
                | RLS enabled on all tables | ✅ |
                | 30-min auto-logout | ✅ |
                | Optional 2FA | ⚠️ |
                | Audit logging (`activity_log`) | ✅ |
                | Error handling (no stack traces) | ✅ |
                | Dependency scanning (`npm audit`) | ✅ |
                | CORS restricted to known origins | ✅ |

                **Password rule**: min 8 chars, uppercase + lowercase + number + special character.

                **CSP**: `default-src 'self'`; allow Supabase, Stripe and Jitsi frames/connections only.
                """
            ),
        )
    )

    files.append(
        (
            "docs/user-guide.md",
            _dedent(
                f"""
                # User Guide — {name}

                {purpose or ''}

                ## Clients
                - Register with your unique Client ID.
                - Submit project requests and track status/progress.
                - Book meetings, message the admin, view and pay invoices.
                - Download deliverables from the Files tab.

                ## Admin
                - Approve/reject requests and update project status.
                - Upload deliverables, manage meetings and block time slots.
                - Generate invoices and monitor revenue analytics.

                ## Tips
                - Press **Cmd/Ctrl+K** anywhere to open the command palette.
                - You are logged out automatically after 30 minutes of inactivity.
                """
            ),
        )
    )

    files.append(
        (
            "README.md",
            _dedent(
                f"""
                # {name}

                {purpose or ''}

                ## Stack
                React 18 + Vite · Tailwind CSS + shadcn/ui · Zustand · React Router v6 ·
                React Hook Form + Zod · Recharts · Supabase (Postgres, Auth, Storage, Edge Functions) ·
                Resend · Stripe · Jitsi Meet.

                ## Structure
                - `frontend/` — React app (Vite).
                - `backend/` — Supabase migrations, RLS policies, seed data and Edge Functions.
                - `docs/` — API, schema, deployment, security and user guide.
                - `tests/` — unit and integration tests.

                ## Quick start
                ```bash
                cd frontend
                npm install
                cp .env.example .env.local   # fill in Supabase + Stripe keys
                npm run dev
                ```
                Set up the backend by running the SQL in `backend/migrations/` and
                `backend/policies/` in Supabase Studio.

                ## Docs
                See `docs/` for schema, API, deployment, security and usage details.
                """
            ),
        )
    )

    files.append(
        (
            ".gitignore",
            _dedent(
                """
                node_modules/
                .env
                .env.local
                .env.*.local
                dist/
                build/
                .vercel/
                .DS_Store
                *.log
                """
            ),
        )
    )

    return files


def build_tests() -> list:
    files = [
        (
            "tests/unit/formatters.test.js",
            _dedent(
                """
                import { describe, it, expect } from 'vitest';
                import { projectNumber } from '@/utils/formatters';

                describe('projectNumber', () => {
                  it('pads to 3 digits', () => {
                    expect(projectNumber(1)).toBe('001');
                    expect(projectNumber(42)).toBe('042');
                  });
                });
                """
            ),
        ),
        (
            "tests/integration/projects.test.js",
            _dedent(
                """
                import { describe, it, expect } from 'vitest';
                import { useProjectStore } from '@/store/projectStore';

                describe('projectStore', () => {
                  it('starts empty', () => {
                    expect(useProjectStore.getState().projects).toEqual([]);
                  });
                });
                """
            ),
        ),
    ]
    return files


# --------------------------------------------------------------------------- #
# Top-level orchestrator
# --------------------------------------------------------------------------- #
def build(parsed: dict) -> dict:
    """Return {relative_path: content} for the whole generated project."""
    out = {}
    tables = parsed["tables"]

    for path, content in build_migrations(tables):
        out[path] = content
    for path, content in build_policies(tables):
        out[path] = content
    for path, content in build_edge_functions():
        out[path] = content
    for path, content in build_seed(tables):
        out[path] = content
    for path, content in build_frontend(parsed["slug"], tables):
        out[path] = content
    for path, content in build_docs(parsed):
        out[path] = content
    for path, content in build_tests():
        out[path] = content

    # Prefix every path with the root folder name from the doc.
    root = parsed["root_name"] or parsed["slug"]
    return {f"{root}/{path}": content for path, content in out.items()}
