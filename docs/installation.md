# Installation

> Part of the [documentation](../README.md#documentation)

Retentacle is distributed as a regular Python package, and as a container
image. Installing the package puts the `retentacle` command on your PATH — that
is the command used in every example in these pages (equivalently, invoke it as
`python -m retentacle`).

## From Python packages

```commandline
pip install .
```

Tip: [`pipx`](https://pipx.pypa.io) installs the command into an isolated, own
virtual environment if you prefer not to pollute your system Python.

There are no standalone executables or binary downloads — running the app
requires a Python 3.14+ environment, or use the container image.

For development, install the test dependencies as well:

```commandline
pip install -e ".[tests]"
```

The test suite is the usual `pytest` run, and the code is formatted with `ruff`:

```commandline
pytest tests
ruff check retentacle tests && ruff format --check retentacle tests
```

## As a container

Pull and start the published multi-architecture image:

```commandline
docker pull ghcr.io/exxamalte/retentacle:main
docker run -d --name retentacle -p 8080:8080 -v retentacle-data:/data \
  ghcr.io/exxamalte/retentacle:main
```

Then open `http://localhost:8080` and complete the first-run setup wizard to
create the admin account.

Building from a checkout is also possible — the build context must be the
repository root, while the Dockerfile lives in `docker/`:

```commandline
docker build -t retentacle -f docker/Dockerfile .
docker run -d --name retentacle -p 8080:8080 -v retentacle-data:/data retentacle
```

Or with Docker Compose:

```commandline
docker compose -f docker/docker-compose.yml up -d --build
```

Registry tags, the data folder, `PUID`/`PGID`, reverse proxies and permission
troubleshooting are covered in
[Docker deployment](docker.md).

## First start

Start the server:

```commandline
retentacle
```

Alternatively, run it as a Python module:

```commandline
python -m retentacle
```

By default the web server listens on `http://127.0.0.1:8080`. Open that address
in a browser for the first time to create the initial admin account — you are
then signed in automatically. Sign in, add your device IP addresses on the
**Settings** page (or list them under `ip_addresses` in a
[configuration file](configuration.md#configuration-file)), and the first backup
starts right away; later runs follow the
[schedule](scheduling.md).

To expose the web UI on your local network, bind to all interfaces:

```commandline
retentacle --host 0.0.0.0 --port 8080
```

The server requires a login for all pages except `/health`, so only expose it on
networks you trust. See [Authentication](authentication.md) for accounts, roles
and single sign-on.
