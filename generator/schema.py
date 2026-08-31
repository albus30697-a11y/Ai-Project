"""PostgreSQL schema definitions for the Freelance Dashboard backend.

Each table is described as an ordered list of (column, definition) tuples so the
scaffolder can emit real, working `CREATE TABLE` migrations and RLS policies.
"""

# column definition format: (column_name, "TYPE [CONSTRAINTS]")
TABLES = {
    "profiles": {
        "comment": "Users: admin and clients. user_type + unique client_id drive role access.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("user_type", "text not null check (user_type in ('admin','client'))"),
            ("client_id", "text unique"),
            ("name", "text not null"),
            ("email", "text not null unique"),
            ("company", "text"),
            ("timezone", "text default 'UTC'"),
            ("two_factor_enabled", "boolean default false"),
            ("notification_prefs", "jsonb default '{}'::jsonb"),
            ("created_at", "timestamptz not null default now()"),
            ("updated_at", "timestamptz not null default now()"),
        ],
    },
    "projects": {
        "comment": "A project = a request that moves through Request -> Approved -> In Progress -> Review -> Completed -> Delivered.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("project_number", "text not null unique"),
            ("client_id", "uuid not null references profiles(id) on delete cascade"),
            ("type", "text not null check (type in ('scraping','analysis','both'))"),
            ("status", "text not null default 'request'"),
            ("priority", "text default 'medium'"),
            ("progress", "integer not null default 0 check (progress between 0 and 100)"),
            ("start_date", "date"),
            ("end_date", "date"),
            ("budget", "numeric(12,2)"),
            ("admin_notes", "text"),
            ("rejection_reason", "text"),
            ("created_at", "timestamptz not null default now()"),
            ("updated_at", "timestamptz not null default now()"),
        ],
    },
    "milestones": {
        "comment": "Per-project tasks with ordering and due dates.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("project_id", "uuid not null references projects(id) on delete cascade"),
            ("title", "text not null"),
            ("status", "text not null default 'pending'"),
            ("due_date", "date"),
            ("sort_order", "integer not null default 0"),
            ("created_at", "timestamptz not null default now()"),
        ],
    },
    "project_files": {
        "comment": "Deliverables / attachments with version history (is_latest flags the current version).",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("project_id", "uuid not null references projects(id) on delete cascade"),
            ("storage_path", "text not null"),
            ("size_bytes", "bigint"),
            ("type", "text"),
            ("category", "text"),
            ("version", "integer not null default 1"),
            ("is_latest", "boolean not null default true"),
            ("uploaded_at", "timestamptz not null default now()"),
        ],
    },
    "meetings": {
        "comment": "Meeting requests. Status: pending / approved / rejected / alternate. alternate_times holds proposed slots.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("client_id", "uuid not null references profiles(id) on delete cascade"),
            ("status", "text not null default 'pending'"),
            ("requested_datetime", "timestamptz"),
            ("approved_datetime", "timestamptz"),
            ("duration_minutes", "integer default 30"),
            ("jitsi_link", "text"),
            ("notes", "text"),
            ("alternate_times", "jsonb default '[]'::jsonb"),
            ("created_at", "timestamptz not null default now()"),
            ("updated_at", "timestamptz not null default now()"),
        ],
    },
    "blocked_time_slots": {
        "comment": "Admin 'Busy' periods that block the calendar.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("start_time", "timestamptz not null"),
            ("end_time", "timestamptz not null"),
            ("reason", "text"),
            ("created_at", "timestamptz not null default now()"),
        ],
    },
    "messages": {
        "comment": "In-app messaging between admin and clients (optionally linked to a project).",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("sender_id", "uuid not null references profiles(id) on delete cascade"),
            ("recipient_id", "uuid not null references profiles(id) on delete cascade"),
            ("project_id", "uuid references projects(id) on delete set null"),
            ("body", "text not null"),
            ("read_at", "timestamptz"),
            ("created_at", "timestamptz not null default now()"),
        ],
    },
    "message_attachments": {
        "comment": "Files shared in messages.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("message_id", "uuid not null references messages(id) on delete cascade"),
            ("storage_path", "text not null"),
            ("file_name", "text not null"),
            ("size_bytes", "bigint"),
            ("type", "text"),
        ],
    },
    "invoices": {
        "comment": "Invoices generated by admin, payable via Stripe.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("invoice_number", "text not null unique"),
            ("client_id", "uuid not null references profiles(id) on delete cascade"),
            ("project_id", "uuid references projects(id) on delete set null"),
            ("amount", "numeric(12,2) not null"),
            ("tax", "numeric(12,2) not null default 0"),
            ("total", "numeric(12,2) not null"),
            ("status", "text not null default 'draft'"),
            ("due_date", "date"),
            ("stripe_payment_id", "text"),
            ("payment_method", "text"),
            ("paid_at", "timestamptz"),
            ("created_at", "timestamptz not null default now()"),
        ],
    },
    "notifications": {
        "comment": "Per-user notifications with deep links and read state.",
        "columns": [
            ("id", "uuid primary key default gen_random_uuid()"),
            ("user_id", "uuid not null references profiles(id) on delete cascade"),
            ("type", "text not null"),
            ("title", "text not null"),
            ("message", "text"),
            ("deep_link", "text"),
            ("read", "boolean not null default false"),
            ("created_at", "timestamptz not null default now()"),
        ],
    },
    "activity_log": {
        "comment": "Audit trail: action, entity, IP and user agent.",
        "columns": [
            ("id", "bigint generated always as identity primary key"),
            ("user_id", "uuid references profiles(id) on delete set null"),
            ("action", "text not null"),
            ("entity", "text"),
            ("entity_id", "text"),
            ("ip_address", "text"),
            ("user_agent", "text"),
            ("created_at", "timestamptz not null default now()"),
        ],
    },
    "settings": {
        "comment": "App configuration as key/value JSONB.",
        "columns": [
            ("key", "text primary key"),
            ("value", "jsonb not null default '{}'::jsonb"),
            ("updated_at", "timestamptz not null default now()"),
        ],
    },
}

# Tables that get an auto-updating updated_at trigger.
UPDATED_AT_TRIGGERS = ["profiles", "projects", "meetings"]

# RLS policy map: table -> list of (name, "USING / WITH CHECK" expression)
RLS_POLICIES = {
    "profiles": [
        ("select_own", "id = auth.uid()"),
        ("update_own", "id = auth.uid()"),
    ],
    "projects": [
        ("client_select_own", "client_id = auth.uid()"),
        ("client_insert_request", "client_id = auth.uid()"),
        ("admin_all", "is_admin()"),
    ],
    "milestones": [
        ("select_own", "exists (select 1 from projects p where p.id = project_id and p.client_id = auth.uid())"),
        ("admin_all", "is_admin()"),
    ],
    "project_files": [
        ("select_own", "exists (select 1 from projects p where p.id = project_id and p.client_id = auth.uid())"),
        ("admin_all", "is_admin()"),
    ],
    "meetings": [
        ("client_select_own", "client_id = auth.uid()"),
        ("client_insert_request", "client_id = auth.uid()"),
        ("admin_all", "is_admin()"),
    ],
    "blocked_time_slots": [
        ("select_all", "true"),
        ("admin_all", "is_admin()"),
    ],
    "messages": [
        ("select_participant", "sender_id = auth.uid() or recipient_id = auth.uid()"),
        ("insert_sender", "sender_id = auth.uid()"),
    ],
    "message_attachments": [
        ("select_participant", "exists (select 1 from messages m where m.id = message_id and (m.sender_id = auth.uid() or m.recipient_id = auth.uid()))"),
    ],
    "invoices": [
        ("client_select_own", "client_id = auth.uid()"),
        ("admin_all", "is_admin()"),
    ],
    "notifications": [
        ("select_own", "user_id = auth.uid()"),
        ("update_own", "user_id = auth.uid()"),
    ],
    "activity_log": [
        ("select_own", "user_id = auth.uid()"),
        ("admin_all", "is_admin()"),
    ],
    "settings": [
        ("select_all", "true"),
        ("admin_all", "is_admin()"),
    ],
}
