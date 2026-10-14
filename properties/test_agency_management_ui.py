from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from billing.rules import collect_usage
from billing.services import AccountService

from .models import Agency, AgencyInvitation, AgencyMembership, AuditEvent


class AgencyManagementUITests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ui-owner")
        self.manager = User.objects.create_user("ui-manager")
        self.agent = User.objects.create_user("ui-agent")
        self.outsider = User.objects.create_user("ui-outsider")
        self.agency = Agency.objects.create(owner=self.owner, name="Рабочая команда")
        for user, role in ((self.owner, "owner"), (self.manager, "manager"), (self.agent, "agent")):
            AgencyMembership.objects.create(agency=self.agency, user=user, role=role)
        self.client.force_login(self.owner)

    def test_invitation_states_links_and_seat_counts(self):
        now = timezone.now()
        pending = AgencyInvitation.objects.create(agency=self.agency, role="agent", created_by=self.owner,
                                                   expires_at=now + timedelta(days=7))
        AgencyInvitation.objects.create(agency=self.agency, role="manager", created_by=self.owner,
                                        expires_at=now + timedelta(days=7), accepted_by=self.agent)
        AgencyInvitation.objects.create(agency=self.agency, role="agent", created_by=self.owner,
                                        expires_at=now - timedelta(hours=1))
        AgencyInvitation.objects.create(agency=self.agency, role="agent", created_by=self.owner,
                                        expires_at=now + timedelta(days=7), revoked_at=now)
        response = self.client.get(reverse("agency_dashboard"))
        self.assertEqual(response.context["occupied_seats"], 3)
        self.assertEqual(response.context["pending_seats"], 1)
        self.assertContains(response, "Занято 3")
        self.assertContains(response, "Ожидают 1")
        for status in ("Ожидает", "Принято", "Истекло", "Отозвано"):
            self.assertContains(response, status)
        self.assertContains(response, f'http://testserver{reverse("agency_join", args=[pending.code])}')
        self.assertContains(response, 'data-copy-link=', count=1)
        self.assertEqual(collect_usage(AccountService.ensure_agency(self.agency)).invited_seats, 1)

    def test_revocation_keeps_history_but_invalidates_link_and_releases_seat(self):
        invite = AgencyInvitation.objects.create(agency=self.agency, role="agent", created_by=self.owner,
                                                 expires_at=timezone.now() + timedelta(days=7))
        response = self.client.get(reverse("agency_dashboard"))
        self.assertContains(response, 'data-confirm-title="Отозвать приглашение?"')
        self.assertRedirects(self.client.post(reverse("agency_invite_revoke", args=[invite.pk])), reverse("agency_dashboard"))
        invite.refresh_from_db()
        self.assertIsNotNone(invite.revoked_at)
        self.assertEqual(collect_usage(AccountService.ensure_agency(self.agency)).invited_seats, 0)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("agency_join", args=[invite.code])).status_code, 404)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("agency_dashboard")), "Отозвано")

    def test_role_edit_requires_explicit_submit_and_remove_opens_preview(self):
        membership = AgencyMembership.objects.get(user=self.agent)
        response = self.client.get(reverse("agency_dashboard"))
        self.assertNotContains(response, 'onchange="this.form.submit()"')
        self.assertContains(response, 'Сохранить роль')
        self.assertContains(response, reverse("agency_member_remove", args=[membership.pk]))
        self.assertEqual(self.client.get(reverse("agency_member_remove", args=[membership.pk])).status_code, 200)
        self.assertRedirects(self.client.post(reverse("agency_member_role", args=[membership.pk]), {"role": "manager"}), reverse("agency_dashboard"))
        membership.refresh_from_db()
        self.assertEqual(membership.role, "manager")

    def test_only_owner_can_rename_and_full_history_is_team_scoped(self):
        other = Agency.objects.create(owner=self.outsider, name="Другая команда")
        AgencyMembership.objects.create(agency=other, user=self.outsider, role="owner")
        AuditEvent.objects.create(agency=other, model_name="Чужой маркер", object_pk="1", action="created")
        self.client.force_login(self.manager)
        self.assertNotContains(self.client.get(reverse("agency_dashboard")), reverse("agency_settings"))
        self.assertEqual(self.client.get(reverse("agency_settings")).status_code, 404)
        self.assertEqual(self.client.post(reverse("agency_rename"), {"name": "Чужое имя"}).status_code, 404)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("agency_dashboard")), reverse("agency_settings"))
        self.assertContains(self.client.get(reverse("agency_settings")), 'name="name"')
        self.assertRedirects(self.client.post(reverse("agency_rename"), {"name": "  Новое имя  "}), reverse("agency_settings"))
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.name, "Новое имя")
        history = self.client.get(reverse("agency_history"))
        self.assertContains(history, "Команда переименована")
        self.assertNotContains(history, "Чужой маркер")
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse("agency_history")), "Новое имя")

    def test_dashboard_tabs_keep_attention_outside_and_journal_separate(self):
        AuditEvent.objects.create(agency=self.agency, model_name="Тестовый маркер", object_pk="1", action="created")
        team = self.client.get(reverse("agency_dashboard"))
        self.assertEqual(team.context["active_tab"], "team")
        self.assertContains(team, "Требует внимания")
        self.assertContains(team, "Сотрудники")
        self.assertContains(team, "Приглашения")
        self.assertNotContains(team, "Тестовый маркер")
        journal = self.client.get(reverse("agency_dashboard") + "?tab=journal")
        self.assertEqual(journal.context["active_tab"], "journal")
        self.assertContains(journal, "Требует внимания")
        self.assertContains(journal, "Тестовый маркер")
        self.assertNotContains(journal, 'class="agency-member-row"')
        self.assertNotContains(journal, 'name="role"')
        self.assertEqual(self.client.get(reverse("agency_history")).context["back_url"],
                         reverse("agency_dashboard") + "?tab=journal")
