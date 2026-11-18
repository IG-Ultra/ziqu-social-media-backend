import html
import logging
import smtplib
from email.message import EmailMessage
from typing import Tuple

import httpx

from .config import settings

logger = logging.getLogger("ziqu.email")


def is_email_configured() -> bool:
    has_resend = bool(settings.RESEND_API_KEY)
    has_brevo = bool(settings.BREVO_API_KEY)
    has_smtp = bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASS)
    return has_resend or has_brevo or has_smtp


def mask_email(email: str) -> str:
    clean = (email or "").strip()
    if "@" not in clean:
        return "***"
    local, domain = clean.split("@", 1)
    if len(local) <= 1:
        masked_local = "*"
    elif len(local) == 2:
        masked_local = f"{local[0]}*"
    else:
        masked_local = f"{local[:2]}{'*' * min(5, len(local) - 3)}{local[-1]}"
    return f"{masked_local}@{domain}"


def _resolve_sender_address() -> str:
    return (
        settings.EMAIL_FROM
        or settings.SMTP_FROM
        or settings.RESEND_FROM
        or settings.SMTP_USER
        or "no-reply@ziqu.app"
    )


def _build_reset_email_bodies(
    username: str,
    reset_link: str,
    deep_link: str,
    reset_code: str,
) -> Tuple[str, str]:
    safe_user = html.escape(username)
    safe_link = html.escape(reset_link, quote=True)
    safe_deep = html.escape(deep_link, quote=True)
    safe_code = html.escape(reset_code)

    text_body = (
        f"Hi @{username},\n\n"
        "We received a request to reset your ZIQU account password.\n\n"
        f"Click the link below to set a new password (valid for 15 minutes):\n"
        f"{reset_link}\n\n"
        f"Or enter this 6-digit verification code in the ZIQU app: {reset_code}\n\n"
        "If you did not request a password reset, you can safely ignore this email. "
        "Your password will remain unchanged.\n"
    )

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Reset Your ZIQU Password</title>
</head>
<body style="margin:0;padding:0;background-color:#0b0b12;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#f3f4f6;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color:#0b0b12;padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:520px;background-color:#151522;border:1px solid #26263a;border-radius:16px;padding:32px 24px;">
          <tr>
            <td align="center" style="padding-bottom:20px;">
              <div style="display:inline-block;padding:8px 18px;border-radius:999px;background:linear-gradient(135deg,#8b5cf6,#ec4899);color:#ffffff;font-weight:800;font-size:18px;letter-spacing:1.5px;">
                ZIQU
              </div>
            </td>
          </tr>
          <tr>
            <td style="font-size:20px;font-weight:700;color:#ffffff;padding-bottom:12px;">
              Reset your password
            </td>
          </tr>
          <tr>
            <td style="font-size:15px;line-height:1.6;color:#cbd5e1;padding-bottom:24px;">
              Hi <strong>@{safe_user}</strong>,<br /><br />
              Tap the button below to verify your email and choose a new password for your ZIQU account. This link expires in <strong>15 minutes</strong>.
            </td>
          </tr>
          <tr>
            <td align="center" style="padding-bottom:22px;">
              <a href="{safe_link}" style="display:inline-block;padding:14px 28px;border-radius:999px;background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#ffffff;text-decoration:none;font-weight:700;font-size:15px;">
                Reset Password
              </a>
            </td>
          </tr>
          <tr>
            <td style="background-color:#1d1d2e;border:1px solid #2e2e46;border-radius:12px;padding:16px;text-align:center;">
              <div style="font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#94a3b8;padding-bottom:6px;">
                App Verification Code
              </div>
              <div style="font-size:28px;font-weight:800;letter-spacing:6px;color:#c4b5fd;">
                {safe_code}
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding-top:20px;font-size:12px;line-height:1.6;color:#94a3b8;">
              If the button doesn't open automatically, copy and paste this link into your browser:<br />
              <a href="{safe_link}" style="color:#a78bfa;word-break:break-all;">{safe_link}</a>
              <br /><br />
              On Android with ZIQU installed: <a href="{safe_deep}" style="color:#a78bfa;">Open in ZIQU App</a>
            </td>
          </tr>
          <tr>
            <td style="padding-top:20px;border-top:1px solid #26263a;font-size:12px;line-height:1.5;color:#64748b;">
              Didn't request this? You can safely ignore this email—your account is protected and no changes have been made.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    return text_body, html_body


def _send_via_resend(to_email: str, subject: str, text_body: str, html_body: str) -> Tuple[bool, str]:
    if not settings.RESEND_API_KEY:
        return False, "Resend not configured"
    sender = settings.RESEND_FROM or _resolve_sender_address()
    if sender == "no-reply@ziqu.app" or "@" not in sender:
        sender = "ZIQU <onboarding@resend.dev>"
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": sender,
                    "to": [to_email],
                    "subject": subject,
                    "text": text_body,
                    "html": html_body,
                },
            )
        if 200 <= resp.status_code < 300:
            return True, "resend"
        logger.warning("Resend API error %s: %s", resp.status_code, resp.text[:300])
        return False, f"Resend error ({resp.status_code})"
    except Exception as exc:
        logger.warning("Resend exception: %s", exc)
        return False, str(exc)


def _send_via_brevo(to_email: str, subject: str, text_body: str, html_body: str) -> Tuple[bool, str]:
    if not settings.BREVO_API_KEY:
        return False, "Brevo not configured"
    sender_email = _resolve_sender_address()
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "api-key": settings.BREVO_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "sender": {"name": "ZIQU", "email": sender_email},
                    "to": [{"email": to_email}],
                    "subject": subject,
                    "textContent": text_body,
                    "htmlContent": html_body,
                },
            )
        if 200 <= resp.status_code < 300:
            return True, "brevo"
        logger.warning("Brevo API error %s: %s", resp.status_code, resp.text[:300])
        return False, f"Brevo error ({resp.status_code})"
    except Exception as exc:
        logger.warning("Brevo exception: %s", exc)
        return False, str(exc)


def _send_via_smtp(to_email: str, subject: str, text_body: str, html_body: str) -> Tuple[bool, str]:
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASS):
        return False, "SMTP not configured"

    smtp_pass = settings.SMTP_PASS
    if "gmail.com" in settings.SMTP_HOST.lower():
        smtp_pass = smtp_pass.replace(" ", "")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM or settings.EMAIL_FROM or settings.SMTP_USER
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    use_ssl = settings.SMTP_USE_SSL or settings.SMTP_PORT == 465
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=12) as server:
                server.login(settings.SMTP_USER, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=12) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(settings.SMTP_USER, smtp_pass)
                server.send_message(msg)
        return True, "smtp"
    except Exception as exc:
        logger.warning("SMTP send failed: %s", exc)
        return False, str(exc)


def send_password_reset_email(
    to_email: str,
    username: str,
    reset_link: str,
    deep_link: str,
    reset_code: str,
) -> Tuple[bool, str]:
    subject = "Reset your ZIQU password"
    text_body, html_body = _build_reset_email_bodies(username, reset_link, deep_link, reset_code)

    last_err = "No email provider configured"
    for sender_fn in (_send_via_resend, _send_via_brevo, _send_via_smtp):
        ok, detail = sender_fn(to_email, subject, text_body, html_body)
        if ok:
            return True, detail
        if "not configured" not in detail.lower():
            last_err = detail
    return False, last_err


def render_reset_password_html(
    *,
    token: str = "",
    username: str = "",
    valid: bool = False,
    error_message: str = "",
    deep_link: str = "",
) -> str:
    safe_token = html.escape(token, quote=True)
    safe_user = html.escape(username)
    safe_error = html.escape(error_message)
    safe_deep = html.escape(deep_link, quote=True)

    if not valid:
        body_section = f"""
        <div style="background:#2a1722;border:1px solid #7f1d1d;color:#fecaca;border-radius:12px;padding:16px;font-size:14px;line-height:1.6;">
          {safe_error or "This password reset link is invalid, expired, or has already been used."}
        </div>
        <p style="margin-top:18px;font-size:14px;color:#94a3b8;line-height:1.5;">
          Open the <strong>ZIQU</strong> app and tap <em>Forgot password?</em> to request a fresh reset link.
        </p>
        """
    else:
        open_app_html = (
            f'<a href="{safe_deep}" style="display:block;text-align:center;margin-bottom:18px;padding:12px 16px;border-radius:12px;background:#1e1b2e;border:1px solid #4c1d95;color:#c4b5fd;text-decoration:none;font-weight:600;font-size:14px;">Open directly in ZIQU App</a>'
            if deep_link
            else ""
        )
        body_section = f"""
        <p style="margin:0 0 18px 0;font-size:14px;color:#cbd5e1;line-height:1.5;">
          Setting a new password for <strong>@{safe_user}</strong>.
        </p>
        {open_app_html}
        <div id="statusBox" style="display:none;border-radius:12px;padding:12px 14px;font-size:14px;line-height:1.5;margin-bottom:16px;"></div>
        <form id="resetForm">
          <label style="display:block;font-size:13px;color:#94a3b8;margin-bottom:6px;">New Password</label>
          <input id="pw1" type="password" minlength="6" required placeholder="At least 6 characters"
                 style="width:100%;box-sizing:border-box;padding:13px 14px;border-radius:12px;border:1px solid #2e2e46;background:#0f0f1a;color:#ffffff;font-size:15px;margin-bottom:14px;outline:none;" />
          <label style="display:block;font-size:13px;color:#94a3b8;margin-bottom:6px;">Confirm New Password</label>
          <input id="pw2" type="password" minlength="6" required placeholder="Re-enter new password"
                 style="width:100%;box-sizing:border-box;padding:13px 14px;border-radius:12px;border:1px solid #2e2e46;background:#0f0f1a;color:#ffffff;font-size:15px;margin-bottom:18px;outline:none;" />
          <button id="submitBtn" type="submit"
                  style="width:100%;padding:14px;border:none;border-radius:999px;background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#ffffff;font-size:15px;font-weight:700;cursor:pointer;">
            Update Password
          </button>
        </form>
        <script>
          const form = document.getElementById('resetForm');
          const statusBox = document.getElementById('statusBox');
          const submitBtn = document.getElementById('submitBtn');
          function showStatus(msg, ok) {{
            statusBox.style.display = 'block';
            statusBox.style.background = ok ? '#064e3b' : '#3b1522';
            statusBox.style.border = ok ? '1px solid #10b981' : '1px solid #f43f5e';
            statusBox.style.color = ok ? '#d1fae5' : '#fecdd3';
            statusBox.textContent = msg;
          }}
          form.addEventListener('submit', async (e) => {{
            e.preventDefault();
            const pw1 = document.getElementById('pw1').value;
            const pw2 = document.getElementById('pw2').value;
            if (pw1.length < 6) {{
              showStatus('Password must be at least 6 characters.', false);
              return;
            }}
            if (pw1 !== pw2) {{
              showStatus('Passwords do not match.', false);
              return;
            }}
            submitBtn.disabled = true;
            submitBtn.textContent = 'Updating...';
            try {{
              const resp = await fetch('/api/auth/reset-password-by-token', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ token: {json_dumps_js(token)}, newPassword: pw1 }})
              }});
              const data = await resp.json().catch(() => ({{}}));
              if (resp.ok && data.success) {{
                showStatus(data.message || 'Password updated! You can now log in to ZIQU.', true);
                form.style.display = 'none';
              }} else {{
                showStatus(data.detail || 'Could not reset password. The link may have expired.', false);
                submitBtn.disabled = false;
                submitBtn.textContent = 'Update Password';
              }}
            }} catch (err) {{
              showStatus('Network error. Please try again.', false);
              submitBtn.disabled = false;
              submitBtn.textContent = 'Update Password';
            }}
          }});
        </script>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ZIQU - Reset Password</title>
</head>
<body style="margin:0;padding:24px 16px;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#090910;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#f3f4f6;">
  <div style="width:100%;max-width:420px;background:#151522;border:1px solid #26263a;border-radius:20px;padding:28px 24px;box-shadow:0 20px 50px rgba(0,0,0,0.45);">
    <div style="text-align:center;margin-bottom:20px;">
      <span style="display:inline-block;padding:8px 18px;border-radius:999px;background:linear-gradient(135deg,#8b5cf6,#ec4899);color:#ffffff;font-weight:800;font-size:17px;letter-spacing:1.5px;">ZIQU</span>
      <h1 style="margin:14px 0 0 0;font-size:21px;font-weight:700;color:#ffffff;">Reset Password</h1>
    </div>
    {body_section}
  </div>
</body>
</html>"""


def json_dumps_js(value: str) -> str:
    import json

    # Escape '<' as '\\u003c' so closing script tags cannot break out of inline JS
    return json.dumps(value).replace("<", "\\u003c")
