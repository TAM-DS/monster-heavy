-- Forward-only foundation. Run as schema owner, never as the runtime role.
CREATE SCHEMA monster_heavy;
REVOKE ALL ON SCHEMA monster_heavy FROM PUBLIC;
SET LOCAL search_path = monster_heavy, pg_catalog;

-- Unconstrained NUMERIC deliberately avoids silently rounding money or quantity.
CREATE DOMAIN nonnegative_decimal AS numeric
    CHECK (VALUE >= 0 AND VALUE NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric));
CREATE DOMAIN positive_decimal AS numeric
    CHECK (VALUE > 0 AND VALUE NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric));
CREATE DOMAIN digest AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE TABLE evidence (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('GROUNDING', 'APPROVAL', 'EXECUTION', 'CONSEQUENCE')),
    source text NOT NULL CHECK (length(source) > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    observed_at timestamptz NOT NULL CHECK (isfinite(observed_at)),
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(recorded_at)),
    UNIQUE (id, kind)
);

-- The row itself is policy evidence. Publication schedules activation; no mutable active flag.
CREATE TABLE policy_versions (
    version bigint PRIMARY KEY CHECK (version > 0),
    rules jsonb NOT NULL CHECK (jsonb_typeof(rules) = 'object'),
    digest digest NOT NULL,
    effective_at timestamptz NOT NULL UNIQUE CHECK (isfinite(effective_at)),
    published_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(published_at)),
    publisher_id text NOT NULL CHECK (length(publisher_id) > 0),
    publisher_role text NOT NULL CHECK (length(publisher_role) > 0),
    UNIQUE (version, digest)
);

CREATE TABLE portfolios (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cash nonnegative_decimal NOT NULL,
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(updated_at))
);
CREATE TABLE positions (
    portfolio_id uuid NOT NULL REFERENCES portfolios(id),
    symbol text NOT NULL CHECK (length(symbol) > 0),
    quantity nonnegative_decimal NOT NULL,
    PRIMARY KEY (portfolio_id, symbol)
);

-- Each revision is a distinct immutable proposal identity.
CREATE TABLE proposals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supersedes_id uuid UNIQUE REFERENCES proposals(id),
    portfolio_id uuid NOT NULL REFERENCES portfolios(id),
    symbol text NOT NULL CHECK (length(symbol) > 0),
    side text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity positive_decimal NOT NULL,
    reference_price positive_decimal NOT NULL,
    terms_hash digest NOT NULL,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','SUPERSEDED','EXECUTED')),
    grounding_evidence_id uuid NOT NULL,
    grounding_kind text NOT NULL DEFAULT 'GROUNDING' CHECK (grounding_kind = 'GROUNDING'),
    model_provenance jsonb NOT NULL CHECK (jsonb_typeof(model_provenance) = 'object'),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(created_at)),
    expires_at timestamptz NOT NULL CHECK (isfinite(expires_at) AND expires_at > created_at),
    CHECK (supersedes_id IS DISTINCT FROM id),
    FOREIGN KEY (grounding_evidence_id, grounding_kind) REFERENCES evidence(id, kind),
    UNIQUE (id, terms_hash)
);

CREATE TABLE approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id uuid NOT NULL UNIQUE,
    terms_hash digest NOT NULL,
    decision text NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    actor_id text NOT NULL CHECK (length(actor_id) > 0),
    actor_role text NOT NULL CHECK (length(actor_role) > 0),
    rationale text NOT NULL,
    evidence_id uuid NOT NULL,
    evidence_kind text NOT NULL DEFAULT 'APPROVAL' CHECK (evidence_kind = 'APPROVAL'),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(created_at)),
    FOREIGN KEY (proposal_id, terms_hash) REFERENCES proposals(id, terms_hash),
    FOREIGN KEY (evidence_id, evidence_kind) REFERENCES evidence(id, kind),
    UNIQUE (id, proposal_id, decision)
);

CREATE TABLE execution_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
    proposal_id uuid NOT NULL REFERENCES proposals(id),
    actor_id text NOT NULL CHECK (length(actor_id) > 0),
    actor_role text NOT NULL CHECK (length(actor_role) > 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(created_at)),
    available_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(available_at)),
    lease_owner text,
    lease_expires_at timestamptz CHECK (isfinite(lease_expires_at)),
    completed_at timestamptz CHECK (isfinite(completed_at)),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    UNIQUE (id, proposal_id)
);
CREATE INDEX execution_requests_ready ON execution_requests(available_at, lease_expires_at)
    WHERE completed_at IS NULL;

CREATE TABLE execution_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid NOT NULL,
    proposal_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'RECEIVED'
        CHECK (status IN ('RECEIVED','VERIFYING','ACCEPTED','REJECTED','FAILED')),
    approval_id uuid,
    approval_decision text NOT NULL DEFAULT 'APPROVED' CHECK (approval_decision = 'APPROVED'),
    execution_evidence_id uuid,
    execution_kind text NOT NULL DEFAULT 'EXECUTION' CHECK (execution_kind = 'EXECUTION'),
    policy_version bigint,
    policy_digest digest,
    consequence_evidence_id uuid,
    consequence_kind text NOT NULL DEFAULT 'CONSEQUENCE' CHECK (consequence_kind = 'CONSEQUENCE'),
    reason_code text CHECK (length(reason_code) > 0),
    started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(started_at)),
    finished_at timestamptz CHECK (isfinite(finished_at) AND finished_at >= started_at),
    FOREIGN KEY (request_id, proposal_id) REFERENCES execution_requests(id, proposal_id),
    FOREIGN KEY (approval_id, proposal_id, approval_decision)
        REFERENCES approvals(id, proposal_id, decision),
    FOREIGN KEY (execution_evidence_id, execution_kind) REFERENCES evidence(id, kind),
    FOREIGN KEY (policy_version, policy_digest) REFERENCES policy_versions(version, digest) MATCH FULL,
    FOREIGN KEY (consequence_evidence_id, consequence_kind) REFERENCES evidence(id, kind),
    CHECK ((status IN ('ACCEPTED','REJECTED','FAILED')) = (finished_at IS NOT NULL)),
    CHECK (status NOT IN ('REJECTED','FAILED') OR reason_code IS NOT NULL),
    CHECK (status <> 'ACCEPTED' OR (
        approval_id IS NOT NULL AND execution_evidence_id IS NOT NULL
        AND policy_version IS NOT NULL AND consequence_evidence_id IS NOT NULL
        AND reason_code IS NULL)),
    UNIQUE (id, status)
);
CREATE UNIQUE INDEX one_accepted_consequence_per_proposal
    ON execution_attempts(proposal_id) WHERE status = 'ACCEPTED';
CREATE INDEX execution_attempts_request ON execution_attempts(request_id);

CREATE TABLE decision_ledger (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id uuid NOT NULL UNIQUE,
    outcome text NOT NULL CHECK (outcome IN ('ACCEPTED','REJECTED','FAILED')),
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(recorded_at)),
    FOREIGN KEY (attempt_id, outcome) REFERENCES execution_attempts(id, status)
);

CREATE TABLE outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id uuid NOT NULL REFERENCES decision_ledger(id),
    event_type text NOT NULL CHECK (length(event_type) > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(created_at)),
    available_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(available_at)),
    lease_owner text,
    lease_expires_at timestamptz CHECK (isfinite(lease_expires_at)),
    delivered_at timestamptz CHECK (isfinite(delivered_at)),
    delivery_attempts integer NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX outbox_ready ON outbox(available_at, lease_expires_at) WHERE delivered_at IS NULL;

CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable: % prohibited', TG_TABLE_NAME, TG_OP
        USING ERRCODE = '23514';
END;
$$;

CREATE FUNCTION protect_fields() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := string_to_array(TG_ARGV[0], ',');
BEGIN
    IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
        RAISE EXCEPTION '% immutable fields changed', TG_TABLE_NAME USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

-- Hashes come from PostgreSQL's canonical JSON representation, never caller-supplied digests.
CREATE FUNCTION hash_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.digest := encode(sha256(convert_to(NEW.rules::text, 'UTF8')), 'hex');
    RETURN NEW;
END;
$$;
CREATE TRIGGER policy_digest BEFORE INSERT ON policy_versions
    FOR EACH ROW EXECUTE FUNCTION hash_policy();

CREATE FUNCTION hash_proposal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.terms_hash := encode(sha256(convert_to(jsonb_build_array(
        NEW.portfolio_id, NEW.symbol, NEW.side, NEW.quantity, NEW.reference_price,
        NEW.expires_at AT TIME ZONE 'UTC'
    )::text, 'UTF8')), 'hex');
    RETURN NEW;
END;
$$;
CREATE TRIGGER proposal_digest BEFORE INSERT ON proposals
    FOR EACH ROW EXECUTE FUNCTION hash_proposal();
CREATE TRIGGER proposal_fields BEFORE UPDATE ON proposals
    FOR EACH ROW EXECUTE FUNCTION protect_fields('status');
CREATE TRIGGER request_fields BEFORE UPDATE ON execution_requests
    FOR EACH ROW EXECUTE FUNCTION protect_fields(
        'available_at,lease_owner,lease_expires_at,completed_at');
CREATE TRIGGER outbox_fields BEFORE UPDATE ON outbox
    FOR EACH ROW EXECUTE FUNCTION protect_fields(
        'available_at,lease_owner,lease_expires_at,delivered_at,delivery_attempts');

CREATE FUNCTION protect_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('ACCEPTED', 'REJECTED', 'FAILED') THEN
        RAISE EXCEPTION 'Terminal execution attempt is immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.id <> OLD.id OR NEW.request_id <> OLD.request_id
        OR NEW.proposal_id <> OLD.proposal_id OR NEW.started_at <> OLD.started_at THEN
        RAISE EXCEPTION 'Attempt identity is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.status = 'VERIFYING' AND NEW.status = 'RECEIVED' THEN
        RAISE EXCEPTION 'Attempt cannot move backwards' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER attempt_fields BEFORE UPDATE ON execution_attempts
    FOR EACH ROW EXECUTE FUNCTION protect_attempt();

DO $$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['evidence','policy_versions','approvals','decision_ledger'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON %I
            FOR EACH ROW EXECUTE FUNCTION reject_mutation()', name);
    END LOOP;
    FOREACH name IN ARRAY ARRAY['proposals','execution_requests','execution_attempts','outbox'] LOOP
        EXECUTE format('CREATE TRIGGER no_delete BEFORE DELETE ON %I
            FOR EACH ROW EXECUTE FUNCTION reject_mutation()', name);
    END LOOP;
    FOREACH name IN ARRAY ARRAY['evidence','policy_versions','approvals','decision_ledger',
                               'proposals','execution_requests','execution_attempts','outbox'] LOOP
        EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON %I
            FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()', name);
    END LOOP;
END;
$$;

-- Group role cannot own tables or bypass triggers. Credentials belong to deployment, not migrations.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'monster_heavy_app') THEN
        CREATE ROLE monster_heavy_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    ELSIF EXISTS (SELECT FROM pg_roles WHERE rolname = 'monster_heavy_app'
                  AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolcanlogin OR rolbypassrls)) THEN
        RAISE EXCEPTION 'Existing monster_heavy_app role has unsafe attributes';
    END IF;
END;
$$;
GRANT USAGE ON SCHEMA monster_heavy TO monster_heavy_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA monster_heavy TO monster_heavy_app;
GRANT UPDATE ON portfolios, positions, proposals, execution_requests, execution_attempts, outbox
    TO monster_heavy_app;
GRANT DELETE ON positions TO monster_heavy_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA monster_heavy FROM PUBLIC;
