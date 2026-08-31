# Freelance Data Scraping & Analysis Dashboard — Condensed Documentation

## 1. Project Overview
- **Name**: Freelance Data Scraping & Analysis Dashboard
- **Purpose**: Interactive dashboard for managing client projects, communication, and deliverables
- **Users**: Admin + Multiple Clients (role-based access)

---

## 2. Functional Requirements

| # | Feature | Key Points |
|---|---------|-----------|
| FR-1 | **Authentication** | Register with unique Client ID, login/logout, email password reset, optional 2FA, 30-min auto-logout, role-based access |
| FR-2 | **Client Dashboard** | View projects, submit requests, track status/progress, download deliverables, upload docs, view invoices, book meetings, message admin, notifications, activity timeline, quick stats |
| FR-3 | **Admin Dashboard** | View all clients/projects, approve/reject requests, update status, upload deliverables, manage meetings, send invoices, respond to messages, block time slots, revenue analytics & workload view |
| FR-4 | **Project Management** | Create/edit/archive, milestones & deadlines, progress tracking, workflow: *Request → Approved → In Progress → Review → Completed → Delivered*, attachments, comments, deliverable versioning |
| FR-5 | **Meeting Scheduler** | Client requests time slots; admin accepts/rejects/proposes alternates; calendar with availability, "Busy" blocked slots, conflict detection, email confirmations, timezone handling, auto-generated meeting links, notes |
| FR-6 | **Communication** | In-app messaging with read receipts & file sharing; email notifications for messages, status updates, meeting confirmations, payments, uploads |
| FR-7 | **Payments** | Invoice generation, status tracking, Stripe gateway, payment history, downloadable invoices |
| FR-8 | **File Management** | Secure upload/download (CSV, Excel, PDF, Images, ZIP), 50MB limit, version history, organized by project |

## 3. Non-Functional Requirements
- **Performance**: <3s page load, upload progress, caching, optimized images
- **Security**: HTTPS, password hashing, SQLi/XSS/CSRF protection, login rate limiting, encrypted file storage
- **Reliability**: 99.5% uptime, daily automated backups, error logging & monitoring
- **Usability**: Mobile responsive, max 3 clicks to any feature, accessible, loading states everywhere
- **Scalability**: 100+ concurrent clients, DB optimization, CDN for static assets

---

## 4. Technology Stack

| Layer | Tools |
|-------|-------|
| **Frontend** | React 18 + Vite, Tailwind CSS + shadcn/ui, Zustand, React Router v6, React Hook Form + Zod, Recharts, FullCalendar, Lucide Icons, React Hot Toast |
| **Backend (BaaS)** | Supabase — PostgreSQL, Auth (JWT), Storage, Realtime, Edge Functions |
| **Third-party** | Resend (email), Stripe (payments), Google Analytics 4, Sentry (errors), Jitsi Meet (video) |
| **Hosting** | Vercel (frontend + CDN), Supabase Cloud (backend) |
| **Dev Tools** | VS Code, Git + GitHub, Postman, Figma, Supabase Studio |

---

## 5. File Structure (Top Level)
```
freelance-dashboard/
├── frontend/               # React app
│   ├── public/
│   └── src/
│       ├── assets/         # Images, fonts, icons
│       ├── components/     # ui/ common/ dashboard/ projects/ calendar/ messages/ payments/
│       ├── pages/          # auth/ client/ admin/
│       ├── layouts/        # Auth / Client / Admin / Main layouts
│       ├── hooks/          # useAuth, useProjects, useFileUpload, useDebounce...
│       ├── store/          # Zustand: auth, project, message, notification stores
│       ├── services/       # api, auth, project, meeting, message, payment, file, email
│       ├── utils/          # supabaseClient, validators, formatters, constants
│       ├── config/         # env, routes, theme
│       └── styles/         # Tailwind + global styles
├── backend/                # Supabase
│   ├── migrations/         # users, projects, meetings, messages, invoices (SQL)
│   ├── functions/          # Edge Functions: send-email, generate-invoice, process-payment
│   ├── policies/           # RLS policies (SQL)
│   └── seed.sql
├── docs/                   # API, schema, deployment, security, user guide
├── tests/                  # unit/ + integration/
└── README.md
```

---

## 6. Database Schema (Key Tables)
- **profiles** — user_type (admin/client), unique `client_id`, name, email, company, timezone, 2FA & notification prefs
- **projects** — `project_number` (PRJ-001), client_id FK, type (scraping/analysis/both), status, priority, progress %, dates, budget, admin notes / rejection reason
- **milestones** — per-project tasks with status, due date, order
- **project_files** — storage path, size, type, category, version + `is_latest`
- **meetings** — client FK, status (pending/approved/rejected/alternate), requested/approved datetime, duration, Jitsi link, notes, `alternate_times` JSONB
- **blocked_time_slots** — admin busy periods (start, end, reason)
- **messages / message_attachments** — sender, recipient, optional project link, read receipt, files
- **invoices** — `invoice_number` (INV-001), amounts + tax, status, due date, Stripe ID, payment method
- **notifications** — user, type, title, message, deep link, read state
- **activity_log** — audit trail (action, entity, IP, user agent)
- **settings** — key/value JSONB app config
- **Triggers**: `updated_at` auto-update on profiles, projects, meetings

### Row Level Security (all tables)
- Users see/update **only their own** profile, projects, meetings, invoices, notifications
- Clients create their own project **requests**; **admin can view/update everything**
- Messages visible **only to sender & recipient**

---

## 7. Security Checklist
| Measure | Implementation |
|---------|---------------|
| HTTPS | SSL via Vercel ✅ |
| Password Hashing | Supabase Auth ✅ |
| SQL Injection | Parameterized queries ✅ |
| XSS | Input sanitization + CSP headers ✅ |
| CSRF | SameSite cookies + tokens ✅ |
| Rate Limiting | Login attempt tracking (lockout 15 min) ✅ |
| File Uploads | 50MB max + MIME whitelist ✅ |
| RLS | Enabled on all tables ✅ |
| Env Vars | `.env.local` never committed ✅ |
| Sessions | 30-min auto-logout, refresh 5 min before expiry ✅ |
| 2FA | Optional via Supabase ⚠️ |
| Audit Logging | `activity_log` table ✅ |
| Error Handling | Never expose stack traces ✅ |
| Dependencies | `npm audit` / Snyk ✅ |
| CORS | Restricted to known origins ✅ |

**Password rule**: min 8 chars, uppercase + lowercase + number + special character.
**CSP**: default-src 'self'; allow Supabase, Stripe, Jitsi frames/connections only.

---

## 8. UX / Design Guidelines (Essentials)
- **Philosophy**: *"Elegant by default, interesting where it matters"* — fast + elegant + distinctive. Not a generic AI-generated SaaS look. Inspired by Linear/Raycast/Vercel **principles** (not their visuals).
- **Visual energy levels**:
  - *Low*: navigation, forms, tables, settings (calm, scannable)
  - *Medium*: dashboards, progress, calendar, activity feeds (subtle gradients, tasteful motion)
  - *High*: onboarding, milestones, completion, empty states (expressive, but professional)
- **Foundation**: shadcn/ui + Tailwind + Lucide, customized into a product identity. Use Motion for React only where animation adds meaning. **Avoid by default**: purple-blue gradients, glassmorphism everywhere, glowing borders, decorative blobs, useless charts.
- **Color**: neutrals + one primary accent + semantic status colors; never color-only status indicators.
- **Typography**: 1–2 families (e.g., Geist, Inter, Manrope); strong hierarchy, compact metadata.
- **Layout**: Sidebar + top nav + workspace; compact, aligned, information-dense. Each view gets its own composition (editorial overview, dense task lists, board, timeline, calendar).
- **Keyboard-first**: **Cmd/Ctrl+K** command palette — fuzzy search, grouped commands, create/navigate/actions.
- **Tasks**: open in right-side drawer; **optimistic updates** with rollback + retry on failure.
- **Dashboards**: composition over card grids; every chart must answer a real question.
- **Delight**: micro-interactions, milestone celebrations, meaningful empty states (what's missing / why it matters / what to do), skeletons over spinners.
- **Performance-first**: optimistic UI, caching, pagination, virtualized lists, debounced search, lazy loading, no layout shifts.
- **Responsive**: deliberate mobile layouts (bottom nav / full-screen sheets), not a shrunken desktop.
- **Accessibility**: semantic HTML, keyboard nav, visible focus, contrast, `prefers-reduced-motion` support.
- **80/20 rule**: ~80% clean & productivity-focused, ~20% distinctive visual moments.

---

## 9. Implementation Roadmap

**Phase 1 — Foundation 🏗️**
- Wk 1: Vite + Tailwind + Supabase setup, Git, auth (login/register/reset), protected routes, layouts, shadcn components
- Wk 2: Client & admin dashboards, stats cards, project lists, approval interface, analytics

**Phase 2 — Project Management 📊**
- Request form, project details, status updates, milestones, file upload/download, messaging, realtime notifications, email service, comments

**Phase 3 — Advanced Features 🚀**
- Calendar, meeting requests, conflict detection, time blocking, Jitsi links; Stripe setup, invoice generation, payment tracking & history

**Phase 4 — Polish & Deploy ✨**
- Cross-browser/mobile testing, performance & security audit, deploy to Vercel, custom domain + SSL, docs

---

## 10. Quick Start
```bash
npm create vite@latest freelance-dashboard -- --template react
cd freelance-dashboard && npm install
npm install @supabase/supabase-js react-router-dom zustand react-hot-toast lucide-react
npm install react-hook-form zod @hookform/resolvers
npm install -D tailwindcss postcss autoprefixer && npx tailwindcss init -p
npx shadcn@latest init && npx shadcn@latest add button card input dialog table
npm run dev
# Deploy
npm run build && npm i -g vercel && vercel --prod
```
**Env vars** (never commit `.env.local`): `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_STRIPE_PUBLIC_KEY`, `VITE_APP_URL`.

---

## 11. Critical Reminders ⚠️
1. NEVER commit `.env.local`
2. Enable RLS on all Supabase tables — security first
3. Test on mobile (60% of users)
4. Validate ALL inputs (client + server side)
5. HTTPS everywhere — no exceptions
6. Use error boundaries — no white screens
7. Add loading states for all async actions
8. Log errors (Sentry or similar)
9. Automate database backups
10. Document your code
