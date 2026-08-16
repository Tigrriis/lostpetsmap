"""Outbound email via Resend's HTTP API.

Uses the Resend REST endpoint directly (stdlib urllib) rather than the
``resend`` SDK, to avoid an extra build-time dependency. When no API key is
configured, messages are logged instead of sent, so local development and
un-provisioned environments still work end to end.

``send_email`` is the single seam every caller goes through; to switch
providers, keep its signature and swap the transport.
"""
import json
import urllib.error
import urllib.request

from flask import current_app

import config

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Must be set, and must not look like a script runtime. api.resend.com sits
# behind Cloudflare, which rejects urllib's default "Python-urllib/3.x" with a
# 403 and Cloudflare error 1010 ("browser signature banned") before the request
# ever reaches Resend. Verified directly: the same POST returns 403/1010 with
# that agent and a clean 401 "API key is invalid" with this one.
#
# The failure is nasty because it looks like an auth problem in the log while
# the key is perfectly good, and because mail failures here are swallowed by
# design — so it presents as "no email is arriving" and nothing else.
USER_AGENT = "PetMap/1.0 (+https://lostpetsmap.com)"


def send_email(to: str, subject: str, body: str, reply_to: str | None = None) -> None:
    """Deliver a plain-text email, or log it if Resend isn't configured."""
    if not config.RESEND_API_KEY:
        _log_email(to, subject, body, reply_to)
        return
    _resend_send(to, subject, body, reply_to)


def _resend_send(to: str, subject: str, body: str, reply_to: str | None) -> None:
    message = {
        "from": config.MAIL_FROM,
        "to": [to],
        "subject": subject,
        "text": body.strip(),
    }
    if reply_to:
        message["reply_to"] = reply_to
    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=json.dumps(message).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,      # see the note above — not optional
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        # Resend puts a JSON reason in the body; surface it for debugging.
        detail = exc.read().decode("utf-8", "replace")
        current_app.logger.error("Resend send to %s failed: HTTP %s %s", to, exc.code, detail)
    except (urllib.error.URLError, TimeoutError) as exc:
        current_app.logger.error("Resend send to %s failed: %s", to, exc)
    # Failures are logged, not raised: callers (password reset especially) must
    # return the same neutral response whether or not delivery succeeded, so the
    # endpoint can't be used to probe which addresses have accounts.


def _log_email(to: str, subject: str, body: str, reply_to: str | None) -> None:
    # WARNING, not INFO: under gunicorn the app logger sits at WARNING, so an
    # info() line would never appear. It is also a genuine warning — mail is
    # not being delivered.
    current_app.logger.warning(
        "EMAIL (not sent - no provider configured)\n"
        "  To:       %s\n"
        "  Reply-To: %s\n"
        "  Subject:  %s\n"
        "%s",
        to,
        reply_to or "-",
        subject,
        "\n".join(f"  | {line}" for line in body.strip().splitlines()),
    )


def send_password_reset(to: str, reset_url: str, ttl_minutes: int) -> None:
    """Send the password-reset link for an account."""
    send_email(
        to,
        f"Reset your {config.SITE_NAME} password",
        f"""
Someone asked to reset the password for this email address.

Open the link below to choose a new one. It expires in {ttl_minutes} minutes
and can only be used once.

{reset_url}

If this wasn't you, ignore this email - your password is unchanged.
""",
    )


def mail_is_configured() -> bool:
    """Is there a provider to actually send with?

    Callers use this to avoid gating anything behind a verification email that
    could never arrive — a lockout caused by a missing API key is far worse
    than the spam the gate prevents.
    """
    return bool(config.RESEND_API_KEY)


def send_email_verification(to: str, verify_url: str, ttl_hours: int) -> None:
    """Confirm someone owns the address they signed up with."""
    send_email(
        to,
        f"Confirm your email for {config.SITE_NAME}",
        f"""
Confirm this address so people can reach you about a pet.

{verify_url}

This link is good for {ttl_hours} hours. Until you use it you can still browse
and post, but you won't be able to message anyone or log a sighting on someone
else's report — and nobody can reach you through the site.

If you didn't sign up, ignore this email. No account can be used from this
address without opening the link above.
""",
    )


def send_owner_message(to: str, pet_label: str, pet_url: str,
                       sender_email: str, message: str) -> None:
    """Relay a message from a finder to the person who filed a report.

    The sender's address goes in Reply-To so the owner can answer directly.
    Neither party's address is ever published on the site; this relay is the
    only way one reaches the other.
    """
    send_email(
        to,
        f"[{config.SITE_NAME}] Message about {pet_label}",
        f"""
Someone has sent you a message about your report:

  {pet_label}
  {pet_url}

------------------------------------------------------------
{message.strip()}
------------------------------------------------------------

Reply to this email to answer them directly ({sender_email}).

If this message is abusive or is an obvious scam, do not reply — report it
from the pet's page instead. Be careful: never send money to someone who
claims to have your pet but won't show you a photo or meet in person.
""",
        reply_to=sender_email,
    )


def send_match_alert(to: str, pet_label: str, species: str, distance_km: float,
                     when: str, description: str, sighting_url: str,
                     matches_url: str) -> None:
    """Tell an owner an unidentified animal was reported near their missing pet.

    Careful with the wording: nobody has said this is their pet, and a message
    that reads like good news when it is a coincidence is cruel. It says what
    was seen and where, and leaves the judgement to them.
    """
    send_email(
        to,
        f"[{config.SITE_NAME}] A {species.lower()} was seen near where you lost {pet_label}",
        f"""
Someone reported seeing a {species.lower()} about {distance_km:.1f} km from where
you last saw {pet_label}. Nobody has identified it — it may well not be yours.

  When: {when}
  What they saw: {description or "(no description)"}

  {sighting_url}

If it looks like them, you can add it to your report from your matches page:

  {matches_url}

Whoever posted it did not catch the animal, so there is nobody holding it.
Going to look is the only way to know.
""",
    )


def send_sighting_alert(to: str, pet_label: str, pet_url: str,
                        note: str, when: str) -> None:
    """Tell the owner someone logged a sighting against their report."""
    send_email(
        to,
        f"[{config.SITE_NAME}] New sighting: {pet_label}",
        f"""
Someone reported a sighting of {pet_label}.

  When: {when}
  Note: {note.strip() or "(no note)"}

See it on the map, with the exact location:

  {pet_url}
""",
    )
