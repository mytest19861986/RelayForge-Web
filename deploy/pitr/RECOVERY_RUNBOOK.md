# PostgreSQL 16 recovery

Supply `PGBACKREST_REPO1_CIPHER_PASS` through the service's secret manager for the
pgBackRest repository. Schedule full backups and continuously archive WAL. Set
`archive_timeout` to at most 300 seconds. Monitor archive lag from the latest
successful archived WAL segment. The targets are RPO ≤ 5 minutes and RTO ≤ 30 minutes.

For the separate AES-GCM archive workflow, place a base64-encoded 32-byte key in a
restricted file named by `RF_PITR_KEY_FILE`. Seal base tarballs with a stable unique
label. In `archive_command`, seal `%p` to the archive using `%f` as the authenticated
label. In `restore_command`, open the archive for `%f` to `%p` using the same label.
Use a fresh directory and verify the base archive against its label before extraction.
Verify the required WAL sequence before starting recovery.

Start recovery with PostgreSQL 16 and keep `max_connections`,
`max_worker_processes`, and `max_locks_per_transaction` at least as high as on
the source. Match `max_prepared_transactions` and `max_wal_senders` as well.
Use a recovery target and check replay completion and the restored transaction
before promoting. Time the drill from opening the encrypted base through the
successful verification query. Retain base and required WAL together for at
least 14 days, including the older base necessary to reach the first retained day.
