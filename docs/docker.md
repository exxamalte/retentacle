# Docker deployment

> Part of the [documentation](../README.md#documentation)

Retentacle publishes a multi-architecture (amd64 and arm64) container image to
the GitHub Container Registry under the full name `ghcr.io/exxamalte/retentacle`.
For the quick start see [Installation](installation.md#as-a-container).

The container runs the server as a non-root user, listens on all interfaces,
and reports health through the public `/health` endpoint.

## Image tags

* **Proper releases** (a plain numeric `vX.Y.Z` git tag) publish the version
  (e.g. `2026.11.0`) and update `latest`.
* **Development builds** on the default branch publish the current application
  version from `pyproject.toml` (e.g. `2026.11.0-dev`), plus `main` and
  `sha-<commit>` identifiers — they never touch `latest`.
* **Pre-release tags** (any suffix such as `-alpha`, `-beta`, `-rc`, `-dev`,
  `.dev1` or `b1`) publish only their tag name and `sha-<commit>`, never
  `latest`.

Until the first proper release of a version, `latest` therefore points at the
previous stable build, not the newest one. Use the moving `main` tag, or pin an
exact version such as `2026.11.0-beta2`, to get current code; `main` and tag
builds are published by the *Container* workflow, which runs on every version
tag and can be started manually for `main`.

The Docker files live in the `docker/` folder: a `Dockerfile`, an entrypoint
script, a `docker-compose.yml` and the ignore rules for the build context.

## The data folder

Everything Retentacle stores — `retentacle_backups.json`,
`retentacle_settings.json` and `retentacle_users.json` — lives in `/data` inside
the container. Keep it on a named volume, as in the compose example, or
bind-mount a host folder.

Because a named volume is empty on first use, Docker copies the ownership and
content of the image's `/data` into it, so the container can write immediately —
no host steps required.

With a host folder, the entrypoint takes care of the classic trap: fresh bind
mounts are created root-owned by Docker, so the container starts as root,
chowns the data folder once to `PUID`/`PGID` (default `10001`), and only then
drops privileges. Set `PUID`/`PGID` to your host folder's owner instead — e.g.
`PUID=1000 PGID=1000` — if you want the JSON files directly readable and
editable as your own user.

On read-only or NFS mounts where `chown` cannot work, match `PUID`/`PGID` to the
folder's owner. With UI managers like Portainer, either create a volume in the
Volumes tab and map it to `/data`, or set `PUID`/`PGID` to the owner of your
host folder — no terminal needed in either case.

### Environment

Startup options are configured with environment variables instead of
hand-written commands (full table in
[Configuration](configuration.md#environment-variables)):

| Variable | Default | Purpose |
| --- | --- | --- |
| `RETENTACLE_HOST` | `0.0.0.0` | Bind address inside the container |
| `RETENTACLE_PORT` | `8080` | HTTP port inside the container; the health check follows it, so adjust the port mapping too (e.g. `-e RETENTACLE_PORT=9000 -p 9000:9000`) |
| `RETENTACLE_DATA_DIR` | `/data` | Where the databases are stored. The image pre-creates this default with the right ownership, so an empty named volume mounted at `/data` "just works" (the Docker `VOLUME` instruction cannot follow the variable, so the mount hint stays `/data`) |
| `PUID` / `PGID` | `10001` | User and group the server drops to after preparing the data folder |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse-proxy support, see below |
| `RETENTACLE_DISCOVERY_TIMEOUT` | `30` | Seconds the on-demand device scan waits for mDNS answers |

Any extra CLI flags still work and are appended after the generated ones, e.g.
`docker run ... retentacle --discover` or
`docker run ... -v retentacle-data:/data retentacle --configuration /data/configuration.yaml`.

## Reverse proxy

Terminate TLS at the proxy and let Retentacle serve plain HTTP. Set
`FORWARDED_ALLOW_IPS` so the server trusts the proxy's `X-Forwarded-*` headers:
the proxy's container IP, its Docker network CIDR, or `*` for the simplest
setups. With that, the app sees the original `https` scheme and host name —
session cookies are automatically sent with the `Secure` flag over HTTPS while
plain LAN http access keeps working without it.

Two proxy caveats worth knowing:

* **OIDC**: set the **Callback URL** on the Settings page to your public
  `https://<host>/oidc/callback`, so the OAuth redirect never depends on header
  forwarding.
* **Path prefixes** (serving the app under `https://host/retentacle/`) are not
  supported — use a dedicated hostname/subdomain or port.

Minimal Traefik (label discovery) compose example:

```yaml
services:
  retentacle:
    image: ghcr.io/exxamalte/retentacle:main
    volumes:
      - retentacle-data:/data
    environment:
      FORWARDED_ALLOW_IPS: "*"
    labels:
      - traefik.enable=true
      - traefik.http.routers.retentacle.rule=Host(`backup.example.com`)
      - traefik.http.routers.retentacle.entrypoints=websecure
      - traefik.http.routers.retentacle.tls=true
```

## Device discovery

mDNS discovery needs direct access to the network where the Shelly devices are
located, or an mDNS proxy needs to be configured. Run the container with
`network_mode: host` (see the commented lines in the compose file) or attach it
to a `macvlan` network on that segment. Otherwise configure device IP addresses
in the web UI, which works with plain port mappings. The Settings page flags
this situation on its own: it warns when a device would be reached through a
router, and its **Check discovery** button reports whether mDNS traffic is
audible at all — see
[Configuration](configuration.md#does-discovery-have-a-chance-here).

## One-shot backups

The same image works for cron-style single runs:

```commandline
docker run --rm -v retentacle-data:/data ghcr.io/exxamalte/retentacle:main \
  --once --configuration /data/configuration.yaml
```

## When the data folder cannot be written

`PermissionError: '/data/retentacle_settings.json'` means the server may not
write its databases. Current builds refuse to start in that state instead of
running without persistence, and both the entrypoint and the server say what
they found:

```text
entrypoint: data folder /data 0:0 -> 10001:10001, running as 10001:10001
ERROR:retentacle.cli:Unable to run Retentacle: Data folder /data cannot be used ([Errno 13] Permission denied: '/data/.write-test'). Found: /data is owned by 0:0 mode 0755; the server runs as 10001:10001; /data is a ext4 mount (rw,relatime). Remedy: start the container as root (remove any user: setting) so the entrypoint can take ownership of the folder, or chown the host folder to PUID/PGID (10001:10001).
```

The `Found:` line is the part to look at: it reports who owns the folder, who
the server is running as, the filesystem and mount options, and whether SELinux
is enforcing — which of the causes below applies falls out of it.

Work through the causes in this order:

1. **Check that the container is allowed to start as root.** The ownership
   repair only runs when the entrypoint is root. A `user:` entry in the stack,
   or the **User** field in Portainer's container form, overrides the image and
   the `PUID`/`PGID` variables — the entrypoint then logs
   `running as 10001:10001 without root, leaving /data (owned by 0:0) as is`
   and the server refuses to start. Remove that setting (the server drops
   privileges itself), or make the host folder match the forced user once with
   `chown -R <uid>:<gid> /host/path`. Check with
   `docker inspect <name> --format 'user={{.Config.User}}'`: it must be empty,
   because the image ships no `USER` instruction.

2. **Check that the running image is current.** A tag is only a name, and a
   container manager may keep using a cached build:
   `docker image inspect <image> --format 'user={{.Config.User}} entrypoint={{.Config.Entrypoint}} created={{.Created}}'`.
   An image with `user=retentacle` predates the PUID/PGID entrypoint and can
   never repair a root-owned bind mount; `user=` (empty) plus
   `entrypoint=['/entrypoint.sh']` is what a current image reports. Re-pull
   (Portainer: *Re-pull image*) or rebuild before recreating the container.

3. **Pick the right owner for the files.** With the entrypoint running as root,
   it takes ownership of the folder at every start; set `PUID`/`PGID` to the
   owner of the host folder (e.g. `PUID=1000 PGID=1000`) when the files should
   stay directly readable and editable there.

4. **SELinux (Fedora, RHEL, openSUSE, …).** Run `getenforce` on the host. With
   `Enforcing`, ownership is correct but the security label still denies
   writes — add `:z` to the bind mount (`/host/path:/data:z`), relabel it with
   `chcon -Rt container_file_t /host/path`, or use a named volume.

5. **Read-only or remote mounts.** NFS exports with `root_squash` reject the
   startup `chown` (the entrypoint logs a warning) and read-only mounts reject
   writes entirely. Match `PUID`/`PGID` to the folder owner, fix the export
   mapping, or use a named volume.

6. **No host terminal needed at all:** a named volume. Create one in Portainer
   under *Volumes*, mount it at `/data`, and nothing has to be chowned — the
   image pre-creates `/data` with the right ownership and an empty volume
   inherits it.
