-- 001: stop storing plaintext API keys.
-- Converts existing keys to SHA-256 (from the plaintext column, since bcrypt
-- hashes can't be converted), then drops the plaintext column.
-- Idempotent: safe to run on a fresh or already-migrated database.
-- Run: docker exec -i crypto-alert-postgres psql -U postgres -d cryptoalert < scripts/migrations/001_hash_api_keys.sql

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS api_key_prefix VARCHAR(12);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'users' AND column_name = 'api_key') THEN
        UPDATE users
        SET api_key_hash   = encode(sha256(api_key::bytea), 'hex'),
            api_key_prefix = left(api_key, 12);
        DROP INDEX IF EXISTS ix_users_api_key;
        ALTER TABLE users DROP COLUMN api_key;
    END IF;
END $$;

ALTER TABLE users ALTER COLUMN api_key_prefix SET NOT NULL;
ALTER TABLE users ALTER COLUMN api_key_hash TYPE VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_api_key_hash ON users (api_key_hash);

COMMIT;
