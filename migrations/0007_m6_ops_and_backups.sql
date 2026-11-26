ALTER TABLE app.backup_manifests DROP CONSTRAINT backup_manifests_cipher_alg_check;
ALTER TABLE app.backup_manifests ADD CHECK (
    cipher_alg IN ('PG_BACKREST_AES256_HMAC', 'AGE_CHACHA20_POLY1305', 'AES_256_GCM')
);
GRANT UPDATE (status) ON app.backup_manifests TO relayforge_web;
ALTER TABLE app.backup_manifests ADD COLUMN inline_archive bytea
    CHECK (octet_length(inline_archive) <= 524288);
