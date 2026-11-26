# Accounts, roles and single sign-on

> Part of the [documentation](../README.md#documentation)

## Users and roles

The web server ships with simple built-in user management:

* On the first start — while no user accounts exist — every page redirects to a
  setup wizard where you create the initial **admin** account. You are then
  signed in automatically.
* Accounts are identified by **email address**, which also links them to
  single sign-on identities (see below).
* **Administrators** can do everything: browse and download backups, run
  backups, change settings, and manage user accounts in the Users section of the
  Settings page.
* **Read-only** users can look around: they can browse devices, view the backup
  history, and download backups, but they cannot change any settings or take
  new backups. The admin-only buttons and pages are hidden from them, and the
  server rejects their requests to those routes.
* Additional accounts — with either role — are created by admins on the
  **Settings** page. Passwords must be at least 12 characters long and include
  at least one digit and one special character. They are stored as salted
  PBKDF2 hashes, never in plain text.
* The last admin account can neither be deleted nor demoted, and nobody can
  delete the account they are signed in with. The users table does not offer
  those actions at all — the blocked rows carry an **Only admin** or
  **Signed in** badge that says why, instead of a button that fails on submit.

### Changing a password

* **Your own password** is changed from the avatar menu (**Change password**,
  also at `/account`). That form asks for the current password, the new
  password, and a confirmation. Changing it signs out every other device while
  the one you are using stays signed in.
* **Someone else's password** is set by an admin on the Settings page with
  **Set password** — the recovery path when a colleague forgets theirs, and
  labelled **Set first password** for an account that an identity provider
  created without a local password.
* Accounts without a local password (provisioned through OpenID Connect) get no
  password-change form at all: their sign-in is managed at the provider until an
  admin gives them a password of their own. While a
  single admin exists, the Users table shows an **Only admin** badge where the
  role control would be, with a note below the table explaining how to lift the
  lock — rather than offering a dropdown that fails on submit.

## Sessions

Login sessions are stored server-side and referenced by HTTP-only cookies
(`retentacle_session`). They expire after 30 days. Changing a password signs
that user out of all sessions.

Behind a reverse proxy that terminates TLS, set `FORWARDED_ALLOW_IPS` so the app
recognises the `https` scheme and marks the session cookie `Secure` — see
[Docker deployment](docker.md#reverse-proxy).

## The avatar menu

Signed-in users get an avatar in the top right corner instead of their email
address and a sign-out button. Clicking it opens a menu with the account name
and role at the top, a link to the Settings page (admins only), **Change
password**, the sign-out action, and the app version at the bottom. For an
account without a local password the menu shows **Signed in via provider** in
place of the password link. The avatar shows the profile picture
from the OIDC provider when the provider sends a `picture` claim — either in its
userinfo response or in the ID token — and falls back to the first letter of the
email address otherwise. If the picture stays missing, add the scope your
provider requires for avatars to the **Scopes** field (e.g. Keycloak only sends
`picture` with the `microprofile` scope or a dedicated client scope/mapper); the
claim is picked up on the next OIDC sign-in.

## OpenID Connect single sign-on

The Settings page has a **Single sign-on (OpenID Connect)** section to let users
sign in through an external identity provider:

* Enable it and enter the **issuer URL**, **client ID**, and **client secret**
  of a confidential OIDC client registered for this app. All endpoints are
  discovered automatically from the issuer's well-known OpenID configuration,
  and the authorization code flow is used. Register the redirect URI
  `https://<your-host>/oidc/callback` at the provider; leave the optional
  **Callback URL** field empty to derive it from the current request, or set
  it to the exact registered redirect URI when running behind a reverse proxy.
* Optionally set a **provider name**: the login button then reads
  "Sign in with <name>" instead of "Sign in with your account provider".
* Users are matched to local accounts by their **email claim**. Signing in
  with the same email address as a local account therefore links to it — the
  easiest way to bootstrap an admin: create the local admin account during
  setup with your provider email, then sign in via OIDC.
* Optionally turn on **automatically create accounts for new OIDC users**.
  New accounts are created as read-only; promote them in the Users section.
* Optionally **disable local password sign-in**. The provider connection is
  verified when you save, so an unreachable issuer cannot lock you out; the
  login page then offers only the provider button and local password requests
  are rejected.
* Identity is anchored on the provider's userinfo endpoint, which the app
  calls with the just-exchanged access token; unverified email claims are
  refused.

### Where the secret lives, and how to get back in

Note that the OIDC **client secret is stored in the settings database**
(`retentacle_settings.json`) as part of the web settings, so keep the target
folder protected — see [Data and retention](data.md).

If you disable password sign-in and your provider becomes unreachable, you can
re-enable it by deleting `retentacle_settings.json` (or removing the `oidc`
entry from its `settings` document) and restarting the server — your device
backups remain untouched.
