# Ubuntu 24.04 deployment

Provision Docker Engine with the Compose plugin and keep AppArmor enabled.
Configure SSH public-key access and disable SSH password authentication before
applying the firewall script. Install the service after preparing the checkout,
environment file, and bind-mount directories.

Create `/srv/relayforge/media/run` owned by 10001:10000 and
`/srv/relayforge/media/staging` owned by 10000:10000. Set both directories to
mode 2770. The isolated renderer's production socket is accessible to the
shared application group. In native mode the daemon defaults to mode 0600.
Set `RF_MEDIA_SOCKET_PATH=/media/run/worker.sock` and
`RF_MEDIA_STAGING_ROOT=/media/staging` in `deploy/.env.prod`.

Provision the PostgreSQL database and credentials before starting consumers.
Bootstrap the baseline with an administrative connection directly to postgres
in a controlled migration invocation. Keep that DSN out of the shared runtime
environment. Set passwords for the web, operator and worker roles and install
the SCRAM verifiers in the PgBouncer userlist. Use transaction pooling for
application database connections. Keep the migration connection direct.

Configure SeaweedFS identities in its mounted S3 JSON and create the application
bucket. Configure CORS and a publicly reachable HTTPS upload endpoint before
enabling browser archive uploads. Set the S3 endpoint used by server-side
processes to the private storage service. Set the application hostname and
allowed hosts, the proxy trust flag, Django secret and master KEK. Restrict
access to the runtime environment and credential files.
