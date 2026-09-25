-- Forward-only Phase 5. The foundation and its recorded checksum remain unchanged.
SET LOCAL search_path = monster_heavy, pg_catalog;

ALTER TABLE proposals ADD COLUMN origin text NOT NULL DEFAULT 'MODEL'
    CHECK (origin IN ('MODEL', 'COMPENSATION'));
ALTER TABLE proposals ADD CONSTRAINT compensation_has_no_model
    CHECK (origin <> 'COMPENSATION' OR model_provenance = '{}'::jsonb);

CREATE TABLE compensation_requests (
    proposal_id uuid PRIMARY KEY REFERENCES proposals(id),
    original_attempt_id uuid NOT NULL UNIQUE,
    original_status text NOT NULL DEFAULT 'ACCEPTED' CHECK (original_status = 'ACCEPTED'),
    requester_id text NOT NULL CHECK (length(btrim(requester_id)) > 0),
    requester_role text NOT NULL CHECK (requester_role = 'operator'),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(requested_at)),
    FOREIGN KEY (original_attempt_id, original_status) REFERENCES execution_attempts(id, status)
);

-- Both directions are checked at commit: an operator-origin proposal cannot lose its link,
-- and a link cannot relabel an AI proposal or change the deterministic compensating terms.
CREATE FUNCTION validate_compensation() RETURNS trigger LANGUAGE plpgsql
SET search_path = monster_heavy, pg_catalog AS $$
DECLARE
    target uuid;
    p proposals;
    original proposals;
    c compensation_requests;
    grounding evidence;
BEGIN
    IF TG_TABLE_NAME = 'proposals' THEN
        target := NEW.id;
    ELSE
        target := NEW.proposal_id;
    END IF;
    SELECT * INTO p FROM proposals WHERE id = target;
    SELECT * INTO c FROM compensation_requests WHERE proposal_id = target;
    IF p.origin = 'COMPENSATION' AND c.proposal_id IS NULL THEN
        RAISE EXCEPTION 'Compensation requires original accepted attempt' USING ERRCODE = '23514';
    END IF;
    IF c.proposal_id IS NOT NULL THEN
        SELECT op.* INTO original FROM execution_attempts a
            JOIN proposals op ON op.id = a.proposal_id WHERE a.id = c.original_attempt_id;
        SELECT * INTO grounding FROM evidence WHERE id = p.grounding_evidence_id;
        IF p.origin <> 'COMPENSATION' OR p.portfolio_id <> original.portfolio_id
            OR p.symbol <> original.symbol OR p.quantity <> original.quantity
            OR p.side <> (CASE original.side WHEN 'BUY' THEN 'SELL' ELSE 'BUY' END)
            OR grounding.payload->>'symbol' IS DISTINCT FROM p.symbol
            OR grounding.payload->>'currency' IS DISTINCT FROM
                (SELECT currency FROM portfolios WHERE id = p.portfolio_id)
            OR (grounding.payload->>'price')::numeric IS DISTINCT FROM p.reference_price
            OR grounding.observed_at > p.created_at
            OR grounding.observed_at < p.created_at - interval '60 seconds'
            OR grounding.id = original.grounding_evidence_id THEN
            RAISE EXCEPTION 'Invalid compensating terms or grounding' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER compensation_proposal AFTER INSERT ON proposals
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.origin = 'COMPENSATION')
    EXECUTE FUNCTION validate_compensation();
CREATE CONSTRAINT TRIGGER compensation_link AFTER INSERT ON compensation_requests
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_compensation();

CREATE TABLE control_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid NOT NULL REFERENCES execution_requests(id),
    kind text NOT NULL CHECK (kind IN ('CLAIM','RECLAIM','SUBMISSION_REPLAY','EXECUTION_REPLAY')),
    worker_owner text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(recorded_at)),
    CHECK ((kind IN ('CLAIM','RECLAIM')) = (worker_owner IS NOT NULL))
);
CREATE INDEX control_events_request ON control_events(request_id, kind);

-- Lease accounting is transactional with the scheduling update, even for direct SQL clients.
CREATE FUNCTION record_claim() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = monster_heavy, pg_catalog AS $$
BEGIN
    IF NEW.lease_owner IS NOT NULL AND
        (NEW.lease_owner, NEW.lease_expires_at) IS DISTINCT FROM
        (OLD.lease_owner, OLD.lease_expires_at) THEN
        INSERT INTO control_events(request_id,kind,worker_owner)
        VALUES (NEW.id, CASE WHEN OLD.lease_owner IS NULL THEN 'CLAIM' ELSE 'RECLAIM' END,
                NEW.lease_owner);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER claim_accounting AFTER UPDATE ON execution_requests
    FOR EACH ROW EXECUTE FUNCTION record_claim();

-- Only this narrow function can add replay observations as the runtime role.
CREATE FUNCTION record_replay(target uuid, event_kind text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = monster_heavy, pg_catalog AS $$
BEGIN
    IF event_kind NOT IN ('SUBMISSION_REPLAY', 'EXECUTION_REPLAY') OR event_kind IS NULL THEN
        RAISE EXCEPTION 'Invalid replay event' USING ERRCODE = '23514';
    END IF;
    IF event_kind = 'EXECUTION_REPLAY' AND NOT EXISTS (
        SELECT FROM execution_attempts WHERE request_id = target
        AND status IN ('ACCEPTED','REJECTED')) THEN
        RAISE EXCEPTION 'Replay requires terminal result' USING ERRCODE = '23514';
    END IF;
    INSERT INTO control_events(request_id,kind) VALUES (target,event_kind);
END;
$$;

DO $$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['compensation_requests','control_events'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON %I
            FOR EACH ROW EXECUTE FUNCTION reject_mutation()', name);
        EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON %I
            FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation()', name);
    END LOOP;
END;
$$;
GRANT SELECT, INSERT ON compensation_requests TO monster_heavy_app;
GRANT SELECT ON control_events TO monster_heavy_app;
REVOKE ALL ON FUNCTION validate_compensation(), record_claim(), record_replay(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_replay(uuid,text) TO monster_heavy_app;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'monster_heavy_audit') THEN
        CREATE ROLE monster_heavy_audit NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    ELSIF EXISTS (SELECT FROM pg_roles WHERE rolname = 'monster_heavy_audit'
                  AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolcanlogin OR rolbypassrls)) THEN
        RAISE EXCEPTION 'Existing monster_heavy_audit role has unsafe attributes';
    END IF;
END;
$$;
GRANT USAGE ON SCHEMA monster_heavy TO monster_heavy_audit;
GRANT SELECT ON ALL TABLES IN SCHEMA monster_heavy TO monster_heavy_audit;
GRANT SELECT ON public.schema_migrations TO monster_heavy_audit;
