import csv
import re
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client as BrowserClient, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import AccountEmailRequest, Client, RealtorProfile
from .services.account_email_service import email_verification_token, enqueue_account_email, password_recovery_token, send_pending_account_emails
from .services.account_security_service import AccountRateLimiter
from .services.csv_security import spreadsheet_safe_cell


PASSWORD = "Secure-account-password-123"


class AuthenticationThrottleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("+79990000000", password=PASSWORD, is_staff=True)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(AUTH_RATE_LIMITS={"login": {"ip": 30, "pair": 2, "identity": 20, "window": 900}})
    def test_login_stops_before_password_check_and_returns_retry_header(self):
        for _ in range(2):
            self.assertEqual(self.client.post(reverse("login"), {"username": self.user.username, "password": "wrong"}).status_code, 200)
        with patch("django.contrib.auth.forms.authenticate") as authenticate:
            response = self.client.post(reverse("login"), {"username": self.user.username, "password": PASSWORD})
        authenticate.assert_not_called()
        self.assertContains(response, "Попробуйте немного позже", status_code=429)
        self.assertEqual(response["Retry-After"], "900")
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(AUTH_RATE_LIMITS={"login": {"ip": 30, "pair": 1, "identity": 20, "window": 900}})
    def test_phone_formatting_cannot_bypass_limit(self):
        self.client.post(reverse("login"), {"username": "+7 (999) 000-00-00", "password": "wrong"})
        response = self.client.post(reverse("login"), {"username": "0079990000000", "password": "wrong"})
        self.assertEqual(response.status_code, 429)

    @override_settings(AUTH_RATE_LIMITS={"login": {"ip": 2, "pair": 10, "identity": 20, "window": 900}})
    def test_changing_identifiers_does_not_bypass_ip_limit(self):
        for index in range(2):
            self.client.post(reverse("login"), {"username": f"+7999000000{index}", "password": "wrong"})
        self.assertEqual(self.client.post(reverse("login"), {"username": "+79990000009", "password": "wrong"}).status_code, 429)

    @override_settings(AUTH_RATE_LIMITS={"login": {"ip": 30, "pair": 10, "identity": 2, "window": 900}})
    def test_distributed_attempts_are_limited_by_account(self):
        for ip in ("192.0.2.1", "192.0.2.2"):
            self.client.post(reverse("login"), {"username": self.user.username, "password": "wrong"}, REMOTE_ADDR=ip)
        self.assertEqual(self.client.post(reverse("login"), {"username": self.user.username, "password": "wrong"}, REMOTE_ADDR="192.0.2.3").status_code, 429)

    @override_settings(AUTH_TRUST_X_FORWARDED_FOR=False, AUTH_RATE_LIMITS={"login": {"ip": 1, "pair": 10, "identity": 20, "window": 900}})
    def test_untrusted_forwarded_header_cannot_bypass_limit(self):
        self.client.post(reverse("login"), {"username": self.user.username, "password": "wrong"}, HTTP_X_FORWARDED_FOR="192.0.2.1")
        self.assertEqual(self.client.post(reverse("login"), {"username": self.user.username, "password": "wrong"}, HTTP_X_FORWARDED_FOR="192.0.2.2").status_code, 429)

    @override_settings(AUTH_RATE_LIMITS={"login": {"ip": 1, "pair": 10, "identity": 20, "window": 900}})
    def test_admin_login_is_also_protected(self):
        self.assertEqual(self.client.post(reverse("admin:login"), {"username": self.user.username, "password": "wrong"}).status_code, 200)
        self.assertEqual(self.client.post(reverse("admin:login"), {"username": self.user.username, "password": "wrong"}).status_code, 429)

    @override_settings(AUTH_RATE_LIMITS={"register": {"ip": 1, "window": 3600}})
    def test_registration_is_throttled_even_for_invalid_forms(self):
        self.client.post(reverse("register"), {"phone": "bad"})
        with patch("properties.forms.RegistrationForm.save") as save:
            response = self.client.post(reverse("register"), {"phone": "+79995555555"})
        self.assertEqual(response.status_code, 429)
        save.assert_not_called()
        self.assertEqual(User.objects.count(), 1)

    def test_get_does_not_consume_login_limit_and_normal_login_still_works(self):
        for _ in range(8):
            self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        self.assertRedirects(self.client.post(reverse("login"), {"username": self.user.username, "password": PASSWORD}), reverse("property_list"))

    def test_cache_outage_fails_closed(self):
        with patch("properties.services.account_security_service.cache.add", side_effect=RuntimeError("offline")), self.assertLogs("properties.services.account_security_service", level="ERROR"):
            response = self.client.post(reverse("login"), {"username": self.user.username, "password": PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_cache_keys_do_not_contain_raw_phone_or_ip(self):
        key = AccountRateLimiter.key("login", "pair", "192.0.2.1:+79990000000")
        self.assertNotIn("192.0.2.1", key)
        self.assertNotIn("79990000000", key)

    def test_csrf_protection_is_not_disabled(self):
        response = BrowserClient(enforce_csrf_checks=True).post(reverse("login"), {"username": self.user.username, "password": PASSWORD})
        self.assertEqual(response.status_code, 403)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEFAULT_FROM_EMAIL="accounts@safe.example", ACCOUNT_PUBLIC_BASE_URL="https://safe.example")
class AccountRecoveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("+79990000000", password=PASSWORD)
        cls.profile = RealtorProfile.objects.create(user=cls.user, email="agent@example.com")

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def verified_profile(self):
        self.profile.email_verified_at = timezone.now()
        self.profile.save(update_fields=["email_verified_at"])
        return self.profile

    def verification_url(self):
        return reverse("account_email_confirm", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(self.user.pk)), "token": email_verification_token.make_token(self.user)})

    def reset_url(self):
        return reverse("password_reset_confirm", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(self.user.pk)), "token": password_recovery_token.make_token(self.user)})

    def request_reset(self, phone=None, email="agent@example.com", **extra):
        return self.client.post(reverse("password_reset"), {"phone": phone or self.user.username, "email": email}, **extra)

    def test_unknown_and_unverified_accounts_have_the_same_response_and_queue_shape(self):
        unknown = self.request_reset(phone="+79991111111")
        unverified = self.request_reset()
        self.assertEqual(unknown.status_code, 302)
        self.assertEqual(unknown["Location"], unverified["Location"])
        self.assertEqual(AccountEmailRequest.objects.count(), 2)
        self.assertFalse(AccountEmailRequest.objects.filter(user__isnull=False).exists())
        self.assertEqual(send_pending_account_emails()["discarded"], 2)
        self.assertEqual(len(mail.outbox), 0)

    def test_verified_account_email_is_queued_not_sent_in_request_and_wrong_email_cannot_reset(self):
        self.verified_profile()
        self.assertRedirects(self.request_reset(), reverse("password_reset_done"))
        self.request_reset(email="someone-else@example.com")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(AccountEmailRequest.objects.filter(user=self.user).count(), 1)
        self.assertEqual(send_pending_account_emails(), {"sent": 1, "discarded": 1, "failed": 0})
        self.assertEqual(mail.outbox[0].to, ["agent@example.com"])

    def test_recovery_request_limit_prevents_mail_flooding(self):
        for _ in range(3):
            self.request_reset()
        self.assertEqual(self.request_reset().status_code, 429)
        self.assertEqual(AccountEmailRequest.objects.count(), 3)

    def test_verification_requires_login_and_current_password(self):
        self.assertEqual(self.client.get(reverse("account_email_verify")).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.post(reverse("account_email_verify"), {"current_password": "wrong"})
        self.assertContains(response, "Проверьте текущий пароль")
        self.assertEqual(AccountEmailRequest.objects.count(), 0)
        self.assertRedirects(self.client.post(reverse("account_email_verify"), {"current_password": PASSWORD}), reverse("account_email_sent"))
        self.assertEqual(AccountEmailRequest.objects.get().kind, "verify")

    def test_verification_link_is_canonical_read_only_on_get_and_single_use_on_post(self):
        self.client.force_login(self.user)
        self.client.post(reverse("account_email_verify"), {"current_password": PASSWORD}, HTTP_HOST="testserver")
        send_pending_account_emails()
        link = re.search(r"https://\S+", mail.outbox[0].body).group()
        self.assertTrue(link.startswith("https://safe.example/email-confirm/"))
        self.client.logout()
        path = link.removeprefix("https://safe.example")
        response = self.client.get(path)
        self.assertContains(response, "Подтвердите email")
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.email_verified_at)
        self.assertContains(self.client.post(path), "Email подтверждён")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(self.client.get(path), "Ссылка недействительна")

    def test_complete_password_reset_invalidates_token_and_old_sessions(self):
        self.verified_profile()
        old_browser = BrowserClient()
        old_browser.force_login(self.user)
        self.user.refresh_from_db()
        url = self.reset_url()
        response = self.client.get(url, follow=True)
        self.assertTrue(response.context["validlink"])
        clean_url = response.redirect_chain[-1][0]
        self.assertNotIn(url.rsplit("/", 2)[-2], clean_url)
        response = self.client.post(clean_url, {"new_password1": "New-secure-password-456", "new_password2": "New-secure-password-456"})
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("New-secure-password-456"))
        self.assertFalse(self.user.check_password(PASSWORD))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(self.client.get(url).context["validlink"])
        self.assertEqual(old_browser.get(reverse("property_list")).status_code, 302)

    def test_logged_in_email_confirmation_returns_to_profile_and_preserves_session(self):
        self.client.force_login(self.user)
        url = self.verification_url()
        self.assertContains(self.client.get(url), "Вернуться в личный кабинет")
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, reverse("edit_profile"))
        self.assertContains(response, "Email подтверждён")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.email_verified_at)

    def test_anonymous_email_confirmation_offers_login_without_signing_user_in(self):
        response = self.client.post(self.verification_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Перейти ко входу")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_other_logged_in_account_is_not_switched_by_email_confirmation(self):
        other = User.objects.create_user("+79991112233", password=PASSWORD)
        self.client.force_login(other)
        response = self.client.post(self.verification_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Вернуться в личный кабинет")
        self.assertEqual(int(self.client.session["_auth_user_id"]), other.pk)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.email_verified_at)

    def test_email_confirmation_with_csrf_enforced_on_local_http(self):
        browser = BrowserClient(enforce_csrf_checks=True)
        url = self.verification_url()
        response = browser.get(url)
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        csrf = browser.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(browser.post(url, {"csrfmiddlewaretoken": csrf}, HTTP_ORIGIN="http://testserver").status_code, 200)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.email_verified_at)

    def test_https_email_confirmation_accepts_origin_only_referer_without_origin_header(self):
        browser = BrowserClient(enforce_csrf_checks=True)
        url = self.verification_url()
        response = browser.get(url, secure=True)
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        csrf = browser.cookies[settings.CSRF_COOKIE_NAME].value
        response = browser.post(url, {"csrfmiddlewaretoken": csrf}, secure=True, HTTP_REFERER="https://testserver/")
        self.assertContains(response, "Email подтверждён")

    def test_null_and_foreign_origins_remain_blocked_even_with_valid_csrf_token(self):
        browser = BrowserClient(enforce_csrf_checks=True)
        url = self.verification_url()
        browser.get(url)
        csrf = browser.cookies[settings.CSRF_COOKIE_NAME].value
        for origin in ("null", "https://foreign.example"):
            with self.subTest(origin=origin):
                response = browser.post(url, {"csrfmiddlewaretoken": csrf}, HTTP_ORIGIN=origin)
                self.assertEqual(response.status_code, 403)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.email_verified_at)
        self.assertEqual(browser.post(url, {}, HTTP_ORIGIN="http://testserver").status_code, 403)

    def test_password_reset_form_post_passes_real_csrf_validation_on_https(self):
        self.verified_profile()
        browser = BrowserClient(enforce_csrf_checks=True)
        response = browser.get(self.reset_url(), secure=True, follow=True)
        self.assertTrue(response.context["validlink"])
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        csrf = browser.cookies[settings.CSRF_COOKIE_NAME].value
        clean_url = response.redirect_chain[-1][0]
        response = browser.post(clean_url, {
            "csrfmiddlewaretoken": csrf,
            "new_password1": "New-secure-password-456",
            "new_password2": "New-secure-password-456",
        }, secure=True, HTTP_ORIGIN="https://testserver", HTTP_REFERER="https://testserver/")
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("New-secure-password-456"))

    def test_changed_email_clears_verification_and_invalidates_tokens_and_queued_mail(self):
        self.verified_profile()
        url = self.reset_url()
        enqueue_account_email(self.user, "reset")
        self.profile.email = "new@example.com"
        self.profile.save(update_fields=["email"])
        self.assertIsNone(self.profile.email_verified_at)
        self.assertFalse(self.client.get(url).context["validlink"])
        self.assertEqual(send_pending_account_emails()["discarded"], 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_old_verification_link_is_invalid_after_email_change(self):
        url = self.verification_url()
        self.profile.email = "new@example.com"
        self.profile.save()
        self.assertContains(self.client.post(url), "Ссылка недействительна")
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.email_verified_at)

    def test_reset_token_expires_and_inactive_account_cannot_recover(self):
        self.verified_profile()
        now = password_recovery_token._now()
        with patch.object(password_recovery_token, "_now", return_value=now):
            token = password_recovery_token.make_token(self.user)
        with patch.object(password_recovery_token, "_now", return_value=now + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 1)):
            self.assertFalse(password_recovery_token.check_token(self.user, token))
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertFalse(password_recovery_token.check_token(self.user, token))

    def test_mail_delivery_retries_are_bounded_and_successful_items_are_not_resent(self):
        self.verified_profile()
        item = enqueue_account_email(self.user, "reset")
        with patch("properties.services.account_email_service.send_mail", side_effect=OSError("SMTP offline")), self.assertLogs("properties.services.account_email_service", level="ERROR"):
            for _ in range(3):
                item.last_attempt_at = timezone.now() - timedelta(minutes=3)
                item.save(update_fields=["last_attempt_at"])
                self.assertEqual(send_pending_account_emails()["failed"], 1)
        item.refresh_from_db()
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.attempts, 3)
        good = enqueue_account_email(self.user, "reset")
        send_pending_account_emails()
        send_pending_account_emails()
        self.assertEqual(len(mail.outbox), 1)
        good.refresh_from_db()
        self.assertEqual(good.status, "sent")

    def test_expired_queue_and_password_change_cancel_pending_mail(self):
        self.verified_profile()
        item = enqueue_account_email(self.user, "reset")
        AccountEmailRequest.objects.filter(pk=item.pk).update(created_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(send_pending_account_emails()["discarded"], 1)
        enqueue_account_email(self.user, "reset")
        self.user.set_password("Another-secure-password-789")
        self.user.save()
        self.assertEqual(send_pending_account_emails()["discarded"], 1)

    @override_settings(ACCOUNT_PUBLIC_BASE_URL="http://safe.example", DEBUG=False)
    def test_command_rejects_non_https_production_origin(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("send_account_emails", stdout=StringIO())

    def test_login_and_profile_expose_recovery_controls(self):
        self.assertContains(self.client.get(reverse("login")), "Забыли пароль?")
        self.client.force_login(self.user)
        response = self.client.get(reverse("edit_profile"))
        self.assertContains(response, "Подтвердить email")
        self.assertContains(response, "Email не подтверждён")
        self.assertContains(response, 'href="#ui-icon-mail"')
        self.assertContains(response, 'aria-labelledby="profile-recovery-title"')
        self.assertNotContains(response, 'class="profile-account__email is-verified"')
        self.verified_profile()
        response = self.client.get(reverse("edit_profile"))
        self.assertContains(response, "Email подтверждён")
        self.assertContains(response, 'class="profile-account__email is-verified"')
        self.assertContains(response, 'href="#ui-icon-check"')
        self.assertContains(response, 'class="profile-account__email-address">agent@example.com</p>')
        self.assertNotContains(response, "Подтвердить email")

    def test_profile_account_without_email_explains_next_step_and_keeps_actions(self):
        self.profile.email = ""
        self.profile.save(update_fields=["email"])
        self.client.force_login(self.user)
        response = self.client.get(reverse("edit_profile"))
        self.assertContains(response, "Email не добавлен")
        self.assertContains(response, "Укажите email в профиле и сохраните изменения.")
        self.assertContains(response, 'href="#ui-icon-mail"')
        self.assertNotContains(response, "Подтвердить email")
        self.assertNotContains(response, 'class="profile-account__email-address"')
        self.assertContains(response, f'<dd>{self.user.username}</dd>', html=True)
        self.assertContains(response, f'href="{reverse("password_change")}"')
        self.assertContains(response, f'action="{reverse("logout")}" class="profile-account__footer"')
        self.assertContains(response, 'href="#ui-icon-log-out"')


class CsvFormulaTests(SimpleTestCase):
    def test_formula_prefixes_controls_whitespace_and_fullwidth_are_neutralized(self):
        for value in ("=1+1", "+79990000000", "-1+1", "@SUM(1)", "\t=1+1", "\r=1+1", "\n=1+1", "  =1+1", "＝1+1", "＋1", "－1", "＠SUM(1)"):
            with self.subTest(value=value):
                self.assertEqual(spreadsheet_safe_cell(value), "\t" + value)

    def test_plain_text_and_numbers_are_unchanged(self):
        for value in ("Анна", "У парка", "", "123", "a,b", 'quote"here'):
            self.assertEqual(spreadsheet_safe_cell(value), value)
        self.assertEqual(spreadsheet_safe_cell(123), "123")


class ClientCsvSecurityTests(TestCase):
    def test_export_quotes_cells_preserves_boundaries_and_workspace_isolation(self):
        owner = User.objects.create_user("csv-owner")
        other = User.objects.create_user("csv-other")
        Client.objects.create(owner=owner, name='=HYPERLINK("evil");,"attack"', phone="+79990000000", preferred_area="\n=1+1", preferred_contact_time="＠SUM(1)")
        Client.objects.create(owner=other, name="Чужой клиент")
        self.client.force_login(owner)
        response = self.client.get(reverse("client_export"))
        data = response.content.decode("utf-8-sig")
        rows = list(csv.reader(StringIO(data)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[1]), 8)
        self.assertTrue(rows[1][0].startswith("\t="))
        self.assertEqual(rows[1][1], "\t+79990000000")
        self.assertTrue(rows[1][5].startswith("\t\n="))
        self.assertNotIn("Чужой клиент", data)
        self.assertTrue(data.startswith('"Имя","Телефон"'))
