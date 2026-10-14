from io import BytesIO, StringIO
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from PIL import Image

from .models import AIContent, Agency, AgencyInvitation, AgencyMembership, AuditEvent, CRMChangeEvent, Client, ClientReminder, Deal, Lead, Property, PropertyImage, RealtorProfile, Showing, UserLegalAcceptance
from .forms import AIContentEditForm, AIRequestForm
from .ai_views import get_ai_history_context
from .services.ai_service import AIService, AIServiceError
from .services.lead_notification_service import LeadNotificationService


class NavigationStructureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("navigation-user", password="password")
        Property.objects.create(owner=self.user, title="Объект для навигации")
        self.client.force_login(self.user)

    def test_sidebar_groups_keep_tasks_and_direct_page_links(self):
        response = self.client.get(reverse("property_list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        primary_nav = html.split('<nav class="sidebar-main-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(primary_nav.count('<a class="sidebar-link'), 8)
        self.assertIn('>Задачи</span>', primary_nav)
        self.assertIn('>Недвижимость</span>', primary_nav)
        self.assertIn('>CRM</span>', primary_nav)
        self.assertIn('>Интеграции</span>', primary_nav)
        self.assertIn(f'href="{reverse("landing_list")}"', primary_nav)
        self.assertIn(f'href="{reverse("integration_telegram")}"', primary_nav)
        self.assertIn(f'href="{reverse("analytics")}"', primary_nav)
        self.assertNotIn(f'href="{reverse("agency_dashboard")}"', primary_nav)
        self.assertIn("reaicrmSidebar('realty')", html)
        self.assertIn('class="sidebar-rail-toggle"', html)
        self.assertIn('aria-label="Задачи', primary_nav)
        self.assertIn("toggleGroup('realty')", primary_nav)
        self.assertIn(f'class="sidebar-link active" href="{reverse("property_list")}"', primary_nav)
        self.assertIn(f'class="sidebar-link " href="{reverse("landing_list")}"', primary_nav)
        self.assertContains(response, f'href="{reverse("agency_dashboard")}"')
        self.assertContains(response, 'aria-label="Основная навигация"')

    def test_landing_screen_marks_landing_sidebar_link_active(self):
        response = self.client.get(reverse("landing_list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        primary_nav = html.split('<nav class="sidebar-main-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertIn(f'class="sidebar-link active" href="{reverse("landing_list")}"', primary_nav)
        self.assertIn(f'class="sidebar-link " href="{reverse("property_list")}"', primary_nav)
        self.assertIn("reaicrmSidebar('realty')", html)
        self.assertContains(response, 'aria-label="Фильтр лендингов по статусу"')
        self.assertContains(response, 'href="?status=published"')
        self.assertContains(response, 'href="?status=draft"')

    def test_crm_badge_remains_on_group_when_leads_are_collapsed(self):
        property = Property.objects.get(owner=self.user)
        Lead.objects.create(property=property, name="Новый клиент", phone="+79990000001")
        response = self.client.get(reverse("property_list"))
        navigation = response.content.decode().split('<nav class="sidebar-main-nav"', 1)[1].split("</nav>", 1)[0]
        crm_button = navigation.split('aria-controls="sidebar-crm-links"', 1)[1].split("</button>", 1)[0]
        lead_link = navigation.split(f'href="{reverse("lead_list")}"', 1)[1].split("</a>", 1)[0]
        self.assertIn("reaicrmSidebar('realty')", response.content.decode())
        self.assertIn('class="sidebar-counter"', crm_button)
        self.assertIn('>1</span>', crm_button)
        self.assertIn('class="sidebar-counter"', lead_link)
        self.assertIn('>1</span>', lead_link)
        self.assertContains(self.client.get(reverse("lead_list")), "reaicrmSidebar('crm')")

    def test_integration_link_opens_its_group(self):
        response = self.client.get(reverse("integration_telegram"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reaicrmSidebar('integrations')")
        self.assertContains(response, f'href="{reverse("integration_telegram")}" aria-current="page">Telegram</a>')

    def test_property_header_uses_accessible_archive_and_xml_icons(self):
        Property.objects.create(owner=self.user, title="Архивный объект", status="archived")
        response = self.client.get(reverse("property_list"))
        header = response.content.decode().split('<header class="ui-page-header">', 1)[1].split("</header>", 1)[0]
        self.assertIn('data-tooltip="Архив объектов (1)" aria-label="Архив объектов (1)"', header)
        self.assertIn('data-tooltip="Выгрузить XML" aria-label="Выгрузить XML"', header)
        self.assertIn('<use href="#ui-icon-archive"></use>', header)
        self.assertIn('<use href="#ui-icon-download"></use>', header)
        self.assertEqual(header.count('class="btn btn-icon btn-neutral"'), 2)
        self.assertIn('data-bs-toggle="dropdown"', header)
        self.assertIn(f'href="{reverse("avito_export")}"', header)
        self.assertIn(f'href="{reverse("cian_export")}"', header)
        self.assertNotIn("dropdown-toggle", header)
        self.assertNotIn("Экспорт XML</button>", header)
        self.assertNotIn("badge", header)

        archived_response = self.client.get(reverse("property_list"), {"status": "archived"})
        self.assertContains(archived_response, 'data-tooltip="К активным объектам" aria-label="К активным объектам"')
        self.assertContains(archived_response, '<use href="#ui-icon-chevron-left"></use>')

    def test_welcome_screen_still_has_landing_navigation(self):
        newcomer = User.objects.create_user("navigation-newcomer", password="password")
        self.client.force_login(newcomer)
        response = self.client.get(reverse("property_list"))
        self.assertContains(response, "Начать с демо")
        self.assertContains(response, f'href="{reverse("landing_list")}"')
        self.assertContains(response, 'aria-label="Основная навигация"')

    def test_empty_landings_use_the_same_empty_state_as_objects(self):
        newcomer = User.objects.create_user("empty-landings-user", password="password")
        self.client.force_login(newcomer)
        response = self.client.get(reverse("landing_list"))
        self.assertContains(response, '<section class="empty-state" aria-labelledby="empty-landings-title">')
        self.assertContains(response, 'class="empty-state__icon"')
        self.assertContains(response, 'class="empty-state__title" id="empty-landings-title"')
        self.assertContains(response, "Лендингов объектов пока нет")
        self.assertNotContains(response, '<div class="fs-3 mb-2">▱</div>')

    def test_landing_cards_share_property_layout_and_keep_publication_actions(self):
        published = Property.objects.create(owner=self.user, title="Опубликованный объект", landing_published=True)
        response = self.client.get(reverse("landing_list"))

        self.assertContains(response, 'data-catalog-view="landings" data-view="cards"')
        for view in ("cards", "compact", "list"):
            self.assertContains(response, f'data-catalog-option="{view}"')
        self.assertContains(response, 'class="property-card landing-card h-100"', count=2)
        self.assertContains(response, 'data-catalog-card-open', count=2)
        self.assertContains(response, 'class="property-card__footer"', count=2)
        self.assertContains(response, f'href="{reverse("edit_property", args=[published.pk])}#landing-pane"')
        self.assertContains(response, f'href="{reverse("public_landing", args=[published.landing_slug])}"', count=1)
        self.assertContains(response, f'action="{reverse("landing_unpublish", args=[published.pk])}"', count=1)
        self.assertContains(response, "Опубликован")
        self.assertContains(response, "Черновик")

    def test_property_view_switch_and_filters_stay_outside_replaced_results(self):
        full = self.client.get(reverse("property_list"))
        partial = self.client.get(reverse("property_list"), HTTP_HX_REQUEST="true")
        filtered = self.client.get(reverse("property_list"), {"q": "Объект"})

        self.assertContains(full, 'data-catalog-view="properties" data-view="cards"')
        self.assertContains(full, 'data-catalog-card-open', count=1)
        self.assertContains(full, 'id="property-results"')
        self.assertContains(full, 'data-property-count>1</span>')
        self.assertContains(partial, 'data-result-count="1"')
        self.assertContains(full, 'data-bs-target="#property-filter-panel"')
        self.assertContains(full, 'class="collapse" id="property-filter-panel"')
        self.assertContains(filtered, 'aria-label="Поиск и фильтры, активных условий: 1"')
        for view in ("cards", "compact", "list"):
            self.assertContains(full, f'data-catalog-option="{view}"')
            self.assertNotContains(partial, f'data-catalog-option="{view}"')
        self.assertContains(partial, 'class="row g-3 catalog-card-grid"')

    def test_agency_screen_is_available_from_profile_menu(self):
        response = self.client.get(reverse("agency_dashboard"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('href="/agency/"', html.split('class="sidebar-bottom"', 1)[1])
        self.assertIn("reaicrmSidebar('profile')", html)

    def test_sidebar_uses_one_state_for_all_expandable_groups(self):
        response = self.client.get(reverse("property_list"))
        html = response.content.decode()
        for group in ("realty", "crm", "integrations", "profile"):
            self.assertIn(f"@click=\"toggleGroup('{group}')\"", html)
            self.assertIn(f'x-show="openGroup === \'{group}\' && !isRail()"', html)
        self.assertNotIn("realtyOpen", html)
        self.assertNotIn("crmOpen", html)
        self.assertNotIn("integrationsOpen", html)
        self.assertNotIn("profileOpen", html)


class AgencyWorkflowTests(TestCase):
    def setUp(self):
        self.founder = User.objects.create_user("agency-founder", password="password")
        self.agent = User.objects.create_user("agency-agent", password="password")
        self.outsider = User.objects.create_user("agency-outsider", password="password")
        self.property = Property.objects.create(owner=self.founder, title="Общий объект")
        self.client_record = Client.objects.create(owner=self.founder, name="Анна", phone="+79990000111")
        self.lead = Lead.objects.create(property=self.property, client=self.client_record, name="Анна", phone=self.client_record.phone)
        self.deal = Deal.objects.create(owner=self.founder, responsible=self.founder, client=self.client_record, property=self.property, lead=self.lead)
        self.client.force_login(self.founder)

    def create_agency(self):
        response = self.client.post(reverse("agency_create"), {"name": "Агентство Тест"})
        self.assertRedirects(response, reverse("agency_dashboard"))
        return Agency.objects.get(owner=self.founder)

    def invite_and_join(self, role="agent"):
        response = self.client.post(reverse("agency_invite"), {"role": role})
        self.assertRedirects(response, reverse("agency_dashboard"))
        invitation = AgencyInvitation.objects.latest("pk")
        self.client.force_login(self.agent)
        self.assertContains(self.client.get(reverse("agency_join", args=[invitation.code])), invitation.agency.name)
        response = self.client.post(reverse("agency_join", args=[invitation.code]))
        self.assertRedirects(response, reverse("agency_dashboard"))
        return invitation

    def test_empty_my_leads_uses_shared_empty_state_and_can_reset_filter(self):
        self.create_agency()
        self.invite_and_join()

        response = self.client.get(reverse("lead_list"), {"assigned": "mine"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["has_any_leads"])
        self.assertFalse(response.context["leads"])
        self.assertContains(response, 'class="empty-state" aria-labelledby="empty-leads-title"')
        self.assertContains(response, 'class="empty-state__icon"')
        self.assertContains(response, 'class="empty-state__title" id="empty-leads-title"')
        self.assertContains(response, "По выбранным условиям заявок нет")
        self.assertContains(response, f'href="{reverse("lead_list")}" hx-get="{reverse("lead_list")}"')
        self.assertContains(response, "Сбросить фильтры")

    def test_invitation_retry_uses_original_link(self):
        self.create_agency()
        key = str(uuid.uuid4())
        first = self.client.post(reverse("agency_invite"), {"role": "agent", "request_key": key})
        second = self.client.post(reverse("agency_invite"), {"role": "agent", "request_key": key})
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(AgencyInvitation.objects.count(), 1)

    def test_founder_conversion_and_invite_share_data_but_not_other_users_data(self):
        private_property = Property.objects.create(owner=self.agent, title="Личный объект сотрудника")
        agency = self.create_agency()
        self.assertContains(self.client.get(reverse("agency_dashboard")), "Агентство Тест")
        self.property.refresh_from_db()
        self.client_record.refresh_from_db()
        self.deal.refresh_from_db()
        self.assertEqual((self.property.agency, self.client_record.agency, self.deal.agency), (agency, agency, agency))
        invitation = self.invite_and_join()
        self.assertContains(self.client.get(reverse("agency_dashboard") + "?tab=journal"), "История изменений")
        self.assertEqual(invitation.role, "agent")
        self.assertEqual(AgencyMembership.objects.get(user=self.agent).agency, agency)
        self.assertEqual(self.client.get(reverse("property_detail", args=[self.property.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("client_detail", args=[self.client_record.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[self.lead.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("deal_detail", args=[self.deal.pk])).status_code, 200)
        self.client.force_login(self.founder)
        self.assertEqual(self.client.get(reverse("property_detail", args=[private_property.pk])).status_code, 404)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[self.lead.pk])).status_code, 404)
        self.client.post(reverse("agency_create"), {"name": "Другое агентство"})
        self.assertEqual(self.client.get(reverse("property_detail", args=[self.property.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("agency_join", args=[invitation.code])).status_code, 404)

    def test_team_member_can_create_deal_for_shared_client_and_property(self):
        agency = self.create_agency()
        self.invite_and_join()
        second_property = Property.objects.create(owner=self.agent, agency=agency, title="Новый общий объект")
        response = self.client.post(reverse("deal_create"), {
            "client": self.client_record.pk, "property": second_property.pk,
            "stage": "new", "expected_commission": "1000", "outcome_note": "",
        })
        new_deal = Deal.objects.get(property=second_property)
        self.assertRedirects(response, reverse("deal_detail", args=[new_deal.pk]))
        self.assertEqual(new_deal.agency, agency)
        self.assertEqual(new_deal.responsible, self.agent)

    def test_founder_can_assign_a_new_deal_to_an_employee(self):
        agency = self.create_agency()
        self.invite_and_join()
        self.client.force_login(self.founder)
        second_property = Property.objects.create(owner=self.founder, agency=agency, title="Сделка для сотрудника")
        form_page = self.client.get(reverse("deal_create"))
        self.assertContains(form_page, '<label class="form-label" for="id_responsible"')
        self.assertContains(form_page, 'name="responsible"')
        response = self.client.post(reverse("deal_create"), {
            "client": self.client_record.pk, "property": second_property.pk,
            "responsible": self.agent.pk, "stage": "new", "expected_commission": "1000",
        })
        new_deal = Deal.objects.get(property=second_property)
        self.assertRedirects(response, reverse("deal_detail", args=[new_deal.pk]))
        self.assertEqual(new_deal.responsible, self.agent)

    def test_deal_responsible_choices_use_profile_name(self):
        self.create_agency()
        self.invite_and_join()
        RealtorProfile.objects.update_or_create(user=self.agent, defaults={"display_name": "Мария Соколова"})
        self.client.force_login(self.founder)

        response = self.client.get(reverse("deal_detail", args=[self.deal.pk]))

        self.assertContains(response, f'<option value="{self.agent.pk}">Мария Соколова</option>')
        self.assertNotContains(response, f'<option value="{self.agent.pk}">agency-agent</option>')

    def test_team_deal_edit_preserves_or_changes_responsible(self):
        self.create_agency()
        self.invite_and_join()
        self.client.force_login(self.founder)
        self.deal.refresh_from_db()
        self.deal.responsible = self.agent
        self.deal.save(update_fields=["responsible"])
        url = reverse("deal_detail", args=[self.deal.pk])
        self.assertContains(self.client.get(url), 'name="responsible"')
        data = {
            "client": self.client_record.pk, "property": self.property.pk, "lead": self.lead.pk,
            "stage": "in_progress", "expected_commission": "1000", "outcome_note": "",
        }

        self.assertRedirects(self.client.post(url, data), url)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.responsible, self.agent)

        self.assertRedirects(self.client.post(url, {**data, "responsible": self.founder.pk}), url)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.responsible, self.founder)

    def test_deal_reassignment_moves_open_work_to_new_owner_everywhere(self):
        agency = self.create_agency()
        self.invite_and_join()
        self.client.force_login(self.founder)
        self.deal.refresh_from_db()
        midday = timezone.localtime(timezone.now()).replace(hour=12, minute=0, second=0, microsecond=0)
        moved_task = ClientReminder.objects.create(
            owner=self.founder, agency=agency, deal=self.deal,
            text="Обсудить условия сделки", due_at=midday,
        )
        delegated_task = ClientReminder.objects.create(
            owner=self.agent, agency=agency, deal=self.deal,
            text="Подготовить документы", due_at=midday,
        )
        completed_task = ClientReminder.objects.create(
            owner=self.founder, agency=agency, deal=self.deal,
            text="Уже выполнено", due_at=midday, is_done=True, completed_at=timezone.now(),
        )
        planned_showing = Showing.objects.create(
            owner=self.founder, agency=agency, deal=self.deal, starts_at=midday,
        )
        completed_showing = Showing.objects.create(
            owner=self.founder, agency=agency, deal=self.deal, starts_at=midday,
            status="completed", outcome_note="Показ состоялся",
        )
        url = reverse("deal_detail", args=[self.deal.pk])
        response = self.client.post(url, {
            "client": self.client_record.pk, "property": self.property.pk, "lead": self.lead.pk,
            "responsible": self.agent.pk, "stage": "in_progress", "expected_commission": "1000",
        })
        self.assertRedirects(response, url)
        self.deal.refresh_from_db()
        for item in (moved_task, delegated_task, completed_task, planned_showing, completed_showing):
            item.refresh_from_db()
        self.assertEqual(self.deal.responsible, self.agent)
        self.assertEqual(moved_task.owner, self.agent)
        self.assertEqual(delegated_task.owner, self.agent)
        self.assertEqual(completed_task.owner, self.founder)
        self.assertEqual(planned_showing.owner, self.agent)
        self.assertEqual(completed_showing.owner, self.founder)
        self.assertTrue(CRMChangeEvent.objects.filter(resource_type="clientreminder", resource_id=moved_task.pk, field_name="owner", old_value=str(self.founder.pk), new_value=str(self.agent.pk)).exists())
        self.assertTrue(CRMChangeEvent.objects.filter(resource_type="showing", resource_id=planned_showing.pk, field_name="owner", old_value=str(self.founder.pk), new_value=str(self.agent.pk)).exists())
        self.assertContains(self.client.get(url), "agency-agent")
        self.assertContains(self.client.get(reverse("client_detail", args=[self.client_record.pk])), "Ответственный: agency-agent")
        self.assertNotContains(self.client.get(reverse("workday")), "Обсудить условия сделки")
        self.client.force_login(self.agent)
        workday = self.client.get(reverse("workday"))
        self.assertContains(workday, "Обсудить условия сделки")
        self.assertContains(workday, reverse("showing_detail", args=[planned_showing.pk]))

    def test_deal_from_team_lead_inherits_its_responsible(self):
        agency = self.create_agency()
        self.invite_and_join()
        self.client.force_login(self.founder)
        self.client_record.refresh_from_db()
        second_property = Property.objects.create(owner=self.founder, agency=agency, title="Объект заявки")
        lead = Lead.objects.create(
            property=second_property, client=self.client_record, name="Анна", phone=self.client_record.phone,
            assigned_to=self.agent, qualification="qualified", first_contacted_at=timezone.now(),
        )
        url = f"{reverse('deal_create')}?lead={lead.pk}"
        page = self.client.get(url)
        self.assertEqual(page.context["form"]["responsible"].value(), self.agent.pk)

        response = self.client.post(reverse("deal_create"), {
            "client": self.client_record.pk, "property": second_property.pk, "lead": lead.pk,
            "stage": "new", "expected_commission": "1000", "outcome_note": "",
        })
        deal = Deal.objects.get(property=second_property)
        self.assertRedirects(response, reverse("deal_detail", args=[deal.pk]))
        self.assertEqual(deal.responsible, self.agent)

    @override_settings(TURNSTILE_ENABLED=False, EMAIL_NOTIFICATIONS_ENABLED=False, TELEGRAM_NOTIFICATIONS_ENABLED=False)
    def test_public_team_landing_creates_shared_lead_and_reuses_shared_client(self):
        cache.clear()
        agency = self.create_agency()
        self.property.landing_published = True
        self.property.save(update_fields=["landing_published"])
        response = self.client.post(reverse("public_landing", args=[self.property.landing_slug]), {
            "contact_purpose": "viewing", "name": "Анна", "phone": self.client_record.phone,
            "personal_data_consent": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Client.objects.filter(agency=agency, phone=self.client_record.phone).count(), 1)
        newest = Lead.objects.filter(property=self.property).latest("pk")
        self.assertEqual(newest.client, self.client_record)
        self.assertEqual(newest.assigned_to, self.founder)

    def test_only_management_can_assign_leads_and_change_team(self):
        agency = self.create_agency()
        self.invite_and_join()
        Lead.objects.filter(pk=self.lead.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self.lead.refresh_from_db()
        self.assertEqual(self.client.post(reverse("lead_assign", args=[self.lead.pk]), {"assigned_to": self.agent.pk}).status_code, 404)
        self.assertEqual(self.client.post(reverse("agency_invite"), {"role": "manager"}).status_code, 404)
        self.client.force_login(self.founder)
        response = self.client.post(reverse("lead_assign", args=[self.lead.pk]), {"assigned_to": self.agent.pk})
        self.assertRedirects(response, f"{reverse('lead_detail', args=[self.lead.pk])}#lead-about")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, self.agent)
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).owner, self.agent)
        self.assertContains(self.client.get(reverse("client_detail", args=[self.client_record.pk])), "agency-agent")
        self.assertNotIn(self.lead, list(self.client.get(reverse("workday")).context["today_leads"]))
        self.client.force_login(self.agent)
        agent_day = self.client.get(reverse("workday"))
        self.assertIn(self.lead, list(agent_day.context["today_leads"]) + list(agent_day.context["overdue_leads"]))
        self.assertEqual(agency.memberships.count(), 2)

    def test_manager_assignment_handler_and_full_card_remain_available(self):
        agency = self.create_agency()
        self.invite_and_join()
        self.client.force_login(self.founder)
        panel = self.client.get(reverse("lead_list"), {"lead": self.lead.pk})
        self.assertNotContains(panel, "Сменить ответственного")
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), "Сменить ответственного")
        url = reverse("lead_assign", args=[self.lead.pk])
        response = self.client.post(url, {
            "assigned_to": self.agent.pk, "panel_context": "lead_list",
            "panel_list_query": "assigned=mine&page=1",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, self.agent)
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).owner, self.agent)

        invalid = self.client.post(url, {
            "assigned_to": "999999", "panel_context": "lead_list",
            "panel_list_query": "assigned=mine&page=1",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Выберите сотрудника команды.")
        self.assertContains(invalid, 'assigned=mine&amp;page=1')

    def test_agent_can_process_assigned_team_lead_but_cannot_reassign_it(self):
        self.create_agency()
        self.invite_and_join()
        self.client.force_login(self.founder)
        self.client.post(reverse("lead_assign", args=[self.lead.pk]), {"assigned_to": self.agent.pk})
        self.client.force_login(self.agent)

        panel = self.client.get(reverse("lead_list"), {"assigned": "mine", "lead": self.lead.pk})
        self.assertEqual(panel.status_code, 200)
        self.assertContains(panel, "Записать первый контакт")
        self.assertNotContains(panel, "Сменить ответственного")
        self.assertEqual(self.client.post(reverse("lead_assign", args=[self.lead.pk]), {
            "assigned_to": self.founder.pk, "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true").status_code, 404)

        result = self.client.post(reverse("lead_process", args=[self.lead.pk]), {
            "source": "landing", "qualification": "qualified", "contact_method": "call",
            "next_action": "Уточнить время показа",
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "action": "contacted", "panel_context": "lead_list",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(result.status_code, 204)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, self.agent)
        self.assertIsNotNone(self.lead.first_contacted_at)
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).owner, self.agent)

        personal = Property.objects.create(owner=self.founder, title="Личный объект")
        private_lead = Lead.objects.create(property=personal, name="Личный клиент", phone="+79990000333")
        self.assertEqual(self.client.get(reverse("lead_list"), {"lead": private_lead.pk}).status_code, 404)

    def test_removed_member_loses_access_and_assignments_return_to_founder(self):
        agency = self.create_agency()
        self.invite_and_join()
        employee_property = Property.objects.create(owner=self.agent, agency=agency, title="Объект сотрудника")
        employee_client = Client.objects.create(owner=self.agent, agency=agency, name="Клиент сотрудника", phone="+79990000222")
        employee_deal = Deal.objects.create(owner=self.agent, agency=agency, responsible=self.agent, client=employee_client, property=employee_property)
        self.client.force_login(self.founder)
        self.client.post(reverse("lead_assign", args=[self.lead.pk]), {"assigned_to": self.agent.pk})
        member = AgencyMembership.objects.get(user=self.agent)
        remove_url = reverse("agency_member_remove", args=[member.pk])
        preview = self.client.get(remove_url)
        response = self.client.post(remove_url, {
            "replacement": self.founder.pk, "preview_token": preview.context["preview_token"],
            "confirm": "transfer_and_remove",
        })
        self.assertRedirects(response, reverse("agency_dashboard"))
        self.lead.refresh_from_db()
        employee_property.refresh_from_db()
        employee_client.refresh_from_db()
        employee_deal.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, agency.owner)
        self.assertEqual(ClientReminder.objects.get(lead=self.lead, is_done=False).owner, agency.owner)
        self.assertEqual((employee_property.owner, employee_client.owner, employee_deal.owner, employee_deal.responsible), (self.founder,) * 4)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("property_detail", args=[self.property.pk])).status_code, 404)

    def test_audit_records_actor_and_changed_fields_without_phone_values(self):
        agency = self.create_agency()
        self.invite_and_join()
        response = self.client.post(reverse("client_status_update", args=[self.client_record.pk]), {"status": "in_progress"})
        self.assertRedirects(response, reverse("client_detail", args=[self.client_record.pk]))
        event = AuditEvent.objects.filter(agency=agency, model_name="Клиент", object_pk=str(self.client_record.pk), action="updated").latest("pk")
        self.assertEqual(event.actor, self.agent)
        self.assertIn("Отношения с клиентом", event.changed_fields)
        self.assertNotIn(self.client_record.phone, str(event.changed_fields))

    def test_dashboard_journal_and_full_history_are_paginated(self):
        agency = self.create_agency()
        AuditEvent.objects.filter(agency=agency).delete()
        for number in range(25):
            AuditEvent.objects.create(agency=agency, model_name="Тест", object_pk=str(number), action="created")

        response = self.client.get(reverse("agency_dashboard") + "?tab=journal")
        self.assertEqual(len(response.context["audit_page"]), 20)
        self.assertContains(response, 'aria-label="Страницы журнала команды"')
        self.assertContains(response, '?tab=journal&amp;page=2')
        self.assertEqual(len(self.client.get(reverse("agency_dashboard") + "?tab=journal&page=2").context["audit_page"]), 5)

        newest = AuditEvent.objects.create(agency=agency, model_name="Тест", object_pk="25", action="created")
        response = self.client.get(reverse("agency_history"))
        self.assertEqual(len(response.context["page"]), 25)
        self.assertEqual(response.context["page"][0].pk, newest.pk)
        self.assertContains(response, 'href="?page=2"')
        response = self.client.get(reverse("agency_history") + "?page=2")
        self.assertEqual(len(response.context["page"]), 1)
        self.assertContains(response, 'href="?page=1"')

    def test_team_agent_cannot_delete_shared_property(self):
        self.create_agency()
        self.invite_and_join()
        self.assertEqual(self.client.post(reverse("delete_property", args=[self.property.pk])).status_code, 404)
        self.assertTrue(Property.objects.filter(pk=self.property.pk).exists())

    def test_invitation_can_be_revoked_and_cannot_be_reused(self):
        self.create_agency()
        self.client.post(reverse("agency_invite"), {"role": "agent"})
        invitation = AgencyInvitation.objects.latest("pk")
        response = self.client.post(reverse("agency_invite_revoke", args=[invitation.pk]))
        self.assertRedirects(response, reverse("agency_dashboard"))
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post(reverse("agency_join", args=[invitation.code])).status_code, 404)

    def test_owner_controls_roles_and_manager_cannot_invite_another_manager(self):
        self.create_agency()
        self.invite_and_join()
        member = AgencyMembership.objects.get(user=self.agent)
        self.assertEqual(self.client.post(reverse("agency_member_role", args=[member.pk]), {"role": "manager"}).status_code, 404)
        self.client.force_login(self.founder)
        self.assertRedirects(
            self.client.post(reverse("agency_member_role", args=[member.pk]), {"role": "manager"}),
            reverse("agency_dashboard"),
        )
        member.refresh_from_db()
        self.assertEqual(member.role, "manager")
        self.client.force_login(self.agent)
        before = AgencyInvitation.objects.count()
        self.client.post(reverse("agency_invite"), {"role": "manager"})
        self.assertEqual(AgencyInvitation.objects.count(), before)


class AccountAndPropertyAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        media_dir = TemporaryDirectory()
        self.addCleanup(media_dir.cleanup)
        self.media_root = Path(media_dir.name)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)

    @staticmethod
    def image_upload():
        image = Image.new("RGB", (20, 20), color="white")
        buffer = BytesIO()
        image.save(buffer, format="JPEG")
        return SimpleUploadedFile("property.jpg", buffer.getvalue(), content_type="image/jpeg")

    def test_registration_creates_an_authenticated_user(self):
        response = self.client.post(
            reverse("register"),
            {
                "phone": "+7 (999) 123-45-67",
                "password1": "Secure-agent-password-123",
                "password2": "Secure-agent-password-123",
                "terms_accepted": "on",
                "personal_data_consent": "on",
            },
        )
        self.assertRedirects(response, reverse("property_list"))
        user = User.objects.get(username="+79991234567")
        self.assertTrue(UserLegalAcceptance.objects.filter(user=user).exists())
        self.assertFalse(any(path.is_file() for path in self.media_root.rglob("*")))

    def test_auth_forms_match_workspace_brand_and_include_favicon(self):
        favicon_link = f'<link rel="icon" type="image/svg+xml" sizes="any" href="{static("branding/favicon.svg")}" />'
        self.assertIsNotNone(finders.find("branding/favicon.svg"))
        for url_name in ("login", "register"):
            with self.subTest(page=url_name):
                response = self.client.get(reverse(url_name))
                self.assertContains(response, "<span>RealtorAI</span>", html=True)
                self.assertNotContains(response, "Rieltor AI")
                self.assertContains(response, favicon_link, html=True)

        owner = User.objects.create_user("brand-owner", password="password")
        self.client.force_login(owner)
        self.assertContains(self.client.get(reverse("edit_profile")), favicon_link, html=True)

    def test_agent_cannot_open_another_agents_property(self):
        owner = User.objects.create_user("owner", password="password")
        visitor = User.objects.create_user("visitor", password="password")
        property = Property.objects.create(title="Закрытый объект", owner=owner)
        self.client.force_login(visitor)
        response = self.client.get(reverse("property_detail", args=[property.pk]))
        self.assertEqual(response.status_code, 404)

    def test_incomplete_property_shows_one_setup_hint(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Черновик", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("property_detail", args=[property.pk]))

        self.assertContains(
            response,
            f'Следующий шаг: <a href="{reverse("edit_property", args=[property.pk])}">Укажите цену объекта</a>',
        )

    def test_empty_text_and_landing_sections_show_contextual_hints(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Черновик", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("edit_property", args=[property.pk]))

        self.assertContains(response, "Можно написать текст вручную или получить черновик от ИИ")
        self.assertContains(response, "Проверьте оформление в предпросмотре, затем сохраните объект")

    def test_property_editor_uses_light_sections_and_highlights_invalid_fields(self):
        owner = User.objects.create_user("form-owner", password="password")
        property = Property.objects.create(title="Черновик", owner=owner)
        self.client.force_login(owner)

        response = self.client.post(reverse("edit_property", args=[property.pk]), {"title": ""})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="app-form property-editor"')
        self.assertContains(response, 'class="app-form-error-summary"')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'class="upload-control upload-control--logo"')

    def test_client_and_profile_forms_share_field_and_upload_feedback(self):
        owner = User.objects.create_user("form-owner", password="password")
        self.client.force_login(owner)

        client_response = self.client.post(reverse("client_create"), {"name": "", "phone": "bad"})
        self.assertEqual(client_response.status_code, 200)
        self.assertContains(client_response, 'class="app-form app-form-panel"')
        self.assertContains(client_response, 'class="app-field-error text-danger"')
        self.assertContains(client_response, 'aria-invalid="true"')

        profile_response = self.client.post(reverse("edit_profile"), {"email": "not-an-email"})
        self.assertEqual(profile_response.status_code, 200)
        self.assertContains(profile_response, 'class="upload-control upload-control--avatar"')
        self.assertContains(profile_response, 'aria-invalid="true"')

    def test_auth_and_password_forms_use_shared_validation_styles(self):
        owner = User.objects.create_user("+79990000000", password="Secure-password-123")

        login_response = self.client.post(reverse("login"), {"username": "+79990000000", "password": "wrong"})
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, 'class="app-form-error-summary"')

        register_response = self.client.post(reverse("register"), {"phone": "bad", "password1": "weak", "password2": "mismatch"})
        self.assertEqual(register_response.status_code, 200)
        self.assertContains(register_response, 'class="app-required"')
        self.assertContains(register_response, 'aria-invalid="true"')

        self.client.force_login(owner)
        password_response = self.client.post(reverse("password_change"), {
            "old_password": "wrong", "new_password1": "Another-secure-password-456", "new_password2": "Another-secure-password-456",
        })
        self.assertEqual(password_response.status_code, 200)
        self.assertTemplateUsed(password_response, "properties/password_change_form.html")
        self.assertContains(password_response, 'class="app-form app-form-panel"')
        self.assertContains(password_response, 'aria-invalid="true"')

    def test_photo_upload_requires_a_selected_file(self):
        owner = User.objects.create_user("form-owner", password="password")
        property = Property.objects.create(title="Черновик", owner=owner)
        self.client.force_login(owner)

        response = self.client.post(reverse("upload_images", args=[property.pk]), {})

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Выберите хотя бы одну фотографию", status_code=400)
        self.assertContains(response, 'class="upload-dropzone"', status_code=400)

    def test_owner_can_upload_a_logo_for_the_public_landing(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Черновик", owner=owner, landing_published=True)
        self.client.force_login(owner)

        response = self.client.post(
            reverse("edit_property", args=[property.pk]),
            {
                "title": property.title,
                "property_type": property.property_type,
                "deal_type": property.deal_type,
                "status": property.status,
                "avito_operation": property.avito_operation,
                "currency": property.currency,
                "landing_template": property.landing_template,
                "landing_published": "on",
                "landing_logo": self.image_upload(),
            },
        )

        self.assertRedirects(response, reverse("property_detail", args=[property.pk]))
        property.refresh_from_db()
        self.assertTrue(property.landing_logo)
        response = self.client.get(reverse("public_landing", args=[property.landing_slug]))
        self.assertContains(response, property.landing_logo.url)

    def test_public_landing_has_no_brand_when_a_logo_is_not_uploaded(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Черновик", owner=owner, landing_published=True)

        response = self.client.get(reverse("public_landing", args=[property.landing_slug]))

        self.assertNotContains(response, '<img class="landing-brand-logo"', html=False)
        self.assertNotContains(response, "REAL ESTATE")

    def test_new_user_sees_the_welcome_screen(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)

        response = self.client.get(reverse("property_list"))

        self.assertContains(response, "Объект, лендинг и заявки — в одном месте")
        self.assertContains(response, "Начать с демо")
        self.assertContains(response, "Создать свой объект")

    def test_workspace_loads_the_shared_design_system_layer(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)

        response = self.client.get(reverse("property_list"))

        self.assertRegex(response.content.decode(), r'<link rel="stylesheet" href="/static/css/rieltor-ui(?:\.[0-9a-f]{12})?\.css"')
        self.assertContains(response, 'id="ui-icon-home"')

    def test_lead_status_filter_counts_and_keeps_owner_scope(self):
        owner = User.objects.create_user("lead-owner", password="password")
        other = User.objects.create_user("other-agent", password="password")
        property = Property.objects.create(title="Квартира у парка", owner=owner)
        other_property = Property.objects.create(title="Чужой объект", owner=other)
        new_lead = Lead.objects.create(property=property, name="Новый клиент", phone="+79990000001")
        Lead.objects.create(property=property, name="Прочитанный клиент", phone="+79990000002", status="read")
        Lead.objects.create(property=other_property, name="Чужой клиент", phone="+79990000003")
        self.client.force_login(owner)

        response = self.client.get(reverse("lead_list"), {"status": "new"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status"], "new")
        self.assertEqual(
            [(tab["value"], tab["count"]) for tab in response.context["status_tabs"]],
            [("", 2), ("new", 1), ("read", 1), ("archived", 0)],
        )
        self.assertEqual(list(response.context["leads"]), [new_lead])
        self.assertContains(response, 'hx-push-url="true"')
        self.assertContains(response, 'class="mobile-records lead-mobile-list"')
        self.assertContains(response, 'class="lead-table__name"')
        self.assertContains(response, 'formnovalidate')
        self.assertNotContains(response, "Чужой клиент")

    def test_empty_lead_status_does_not_show_first_use_hint(self):
        owner = User.objects.create_user("lead-owner", password="password")
        property = Property.objects.create(title="Квартира у парка", owner=owner)
        Lead.objects.create(property=property, name="Новый клиент", phone="+79990000001")
        self.client.force_login(owner)

        response = self.client.get(reverse("lead_list"), {"status": "archived"})

        self.assertContains(response, "В этом статусе заявок нет")
        self.assertNotContains(response, "Откройте лендинг объекта и отправьте тестовую заявку")

    def test_client_list_keeps_filters_in_chips_and_pagination(self):
        owner = User.objects.create_user("client-owner", password="password")
        other = User.objects.create_user("other-client-owner", password="password")
        for index in range(11):
            Client.objects.create(owner=owner, name=f"Анна {index:02d}", phone=f"+7999000{index:04d}", status="in_progress", source="landing")
        Client.objects.create(owner=owner, name="Борис", phone="+79995550000", status="new")
        Client.objects.create(owner=other, name="Анна чужая", phone="+79995550001", status="in_progress")
        self.client.force_login(owner)

        response = self.client.get(reverse("client_list"), {
            "q": "Анна", "status": "in_progress", "source": "landing", "sort": "name_asc", "page": "2",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 11)
        self.assertEqual(len(response.context["clients"]), 1)
        self.assertContains(response, "Отношения: В работе")
        self.assertContains(response, "Источник: Лендинг")
        self.assertContains(response, "Сортировка: Имя: А–Я")
        self.assertContains(response, 'class="lead-table-shell desktop-records"')
        self.assertContains(response, 'class="lead-mobile-card client-mobile-card"')
        self.assertIn("status=in_progress", response.context["pagination_query"])
        self.assertIn("source=landing", response.context["pagination_query"])
        search_chip = next(item for item in response.context["active_filters"] if item["label"] == "Поиск: Анна")
        query = parse_qs(urlsplit(search_chip["url"]).query)
        self.assertNotIn("q", query)
        self.assertNotIn("page", query)
        self.assertEqual(query["status"], ["in_progress"])

    def test_client_list_counts_leads_and_sorts_by_count(self):
        owner = User.objects.create_user("client-count-owner", password="password")
        property_record = Property.objects.create(owner=owner, title="Квартира")
        frequent = Client.objects.create(owner=owner, name="Анна", phone="+79990000201")
        occasional = Client.objects.create(owner=owner, name="Борис", phone="+79990000202")
        for index in range(2):
            Lead.objects.create(property=property_record, client=frequent, name="Анна", phone=f"+7999000030{index}")
        Lead.objects.create(property=property_record, client=occasional, name="Борис", phone="+79990000310")
        self.client.force_login(owner)

        response = self.client.get(reverse("client_list"), {"sort": "leads_desc"})

        self.assertEqual([(client.name, client.lead_count) for client in response.context["clients"]],
                         [("Анна", 2), ("Борис", 1)])
        self.assertNotIn("metrics", response.context)

    def test_client_reminder_card_shows_full_count_and_only_first_five(self):
        owner = User.objects.create_user("client-owner", password="password")
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        for index in range(6):
            ClientReminder.objects.create(client=client, text=f"Напоминание {index}", due_at=timezone.now() - timedelta(days=1, minutes=index))
        self.client.force_login(owner)

        response = self.client.get(reverse("client_list"))

        self.assertEqual(response.context["overdue_reminder_count"], 6)
        self.assertContains(response, "Напоминание 5")
        self.assertNotContains(response, "Напоминание 0")
        self.assertContains(response, 'class="client-reminder-group is-overdue"')

    def test_client_board_has_status_counts_and_only_own_clients(self):
        owner = User.objects.create_user("client-owner", password="password")
        other = User.objects.create_user("other-client-owner", password="password")
        Client.objects.create(owner=owner, name="Анна", phone="+79990000000", status="new")
        Client.objects.create(owner=owner, name="Борис", phone="+79990000001", status="nurture")
        Client.objects.create(owner=other, name="Чужой клиент", phone="+79990000002", status="new")
        self.client.force_login(owner)

        response = self.client.get(reverse("client_board"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([(column["key"], column["count"]) for column in response.context["columns"]],
                         [("new", 1), ("in_progress", 0), ("nurture", 1), ("inactive", 0)])
        self.assertContains(response, 'class="client-board__column is-new"')
        self.assertNotContains(response, "Чужой клиент")

    def test_client_board_pages_one_column_without_hiding_other_columns(self):
        owner = User.objects.create_user("board-page-owner", password="password")
        for index in range(13):
            Client.objects.create(owner=owner, name=f"Клиент {index:02d}", phone=f"+7999001{index:04d}")
        Client.objects.create(owner=owner, name="Клиент в сопровождении", phone="+79990019999", status="nurture")
        self.client.force_login(owner)

        first = self.client.get(reverse("client_board"))
        first_new = next(column for column in first.context["columns"] if column["key"] == "new")
        self.assertEqual((first_new["count"], len(first_new["page"])), (13, 12))
        self.assertContains(first, "Клиент 12")
        self.assertNotContains(first, "Клиент 00")
        self.assertContains(first, "Клиент в сопровождении")

        second = self.client.get(first_new["next_url"])
        second_new = next(column for column in second.context["columns"] if column["key"] == "new")
        self.assertEqual((second_new["count"], len(second_new["page"])), (13, 1))
        self.assertContains(second, "Клиент 00")
        self.assertContains(second, "Клиент в сопровождении")

    def test_client_bulk_delete_keeps_safe_filter_url(self):
        owner = User.objects.create_user("client-owner", password="password")
        other = User.objects.create_user("other-client-owner", password="password")
        own_client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000", status="new")
        other_client = Client.objects.create(owner=other, name="Чужой клиент", phone="+79990000001", status="new")
        self.client.force_login(owner)

        response = self.client.post(reverse("client_bulk_action"), {
            "action": "delete", "client_ids": [own_client.pk, other_client.pk], "next": f"{reverse('client_list')}?status=new",
        })

        self.assertRedirects(response, f"{reverse('client_list')}?status=new")
        self.assertFalse(Client.objects.filter(pk=own_client.pk).exists())
        self.assertTrue(Client.objects.filter(pk=other_client.pk).exists())

    def test_property_filter_chips_remove_one_filter_and_keep_the_rest(self):
        owner = User.objects.create_user("agent", password="password")
        matching = Property.objects.create(title="Дом у леса", owner=owner, status="draft", price=100000)
        Property.objects.create(title="Студия у парка", owner=owner, status="draft", price=150000)
        Property.objects.create(title="Квартира в городе", owner=owner, status="published", price=200000)
        self.client.force_login(owner)

        response = self.client.get(reverse("property_list"), {
            "q": "лес", "status": "draft", "sort": "price_desc", "page": "1",
        })

        self.assertContains(response, "Поиск: лес")
        self.assertContains(response, "Статус: Черновик")
        self.assertContains(response, "Сортировка: Цена: по убыванию")
        self.assertContains(response, matching.title)
        self.assertNotContains(response, "Студия у парка")
        self.assertNotContains(response, "Квартира в городе")
        search_chip = next(item for item in response.context["active_filters"] if item["label"] == "Поиск: лес")
        query = parse_qs(urlsplit(search_chip["url"]).query)
        self.assertNotIn("q", query)
        self.assertNotIn("page", query)
        self.assertEqual(query["status"], ["draft"])
        self.assertEqual(query["sort"], ["price_desc"])

        filtered_response = self.client.get(search_chip["url"])
        self.assertContains(filtered_response, "Студия у парка")
        self.assertNotContains(filtered_response, "Квартира в городе")
        self.assertContains(filtered_response, "Дом у леса")

    def test_property_htmx_results_include_chips_and_filtered_empty_state(self):
        owner = User.objects.create_user("agent", password="password")
        Property.objects.create(title="Дом у леса", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("property_list"), {"q": "моря"}, HTTP_HX_REQUEST="true")

        self.assertContains(response, "Поиск: моря")
        self.assertContains(response, "По выбранным условиям объектов нет")
        self.assertContains(response, "Сбросить фильтры")
        self.assertNotContains(response, 'id="property-filters"')

        invalid_price_response = self.client.get(reverse("property_list"), {"min_price": "not-a-price"})
        self.assertEqual(invalid_price_response.status_code, 200)
        self.assertContains(invalid_price_response, "Дом у леса")

    def test_new_user_can_start_with_a_manual_property(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)

        response = self.client.post(reverse("onboarding_start_manual"))

        self.assertRedirects(response, reverse("create_property"), fetch_redirect_response=False)
        self.assertTrue(RealtorProfile.objects.get(user=user).onboarding_started)

    def test_starting_a_property_uses_safe_get_then_creates_a_draft_with_ai_tools(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)

        response = self.client.get(reverse("create_property"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Property.objects.filter(owner=user).exists())
        key = response.context["resource_request_key"]
        form = {"title": "Новый объект", "property_type": "apartment", "deal_type": "sale", "status": "draft",
                "avito_operation": "sell", "currency": "RUB", "landing_template": "classic", "request_key": key}
        response = self.client.post(reverse("create_property"), form)
        draft = Property.objects.get(owner=user)
        self.assertEqual(draft.title, "Новый объект")
        self.assertEqual(draft.status, "draft")
        self.assertRedirects(response, reverse("edit_property", args=[draft.pk]))
        repeat = self.client.post(reverse("create_property"), form)
        self.assertRedirects(repeat, reverse("edit_property", args=[draft.pk]))
        self.assertEqual(Property.objects.filter(owner=user).count(), 1)

        response = self.client.get(reverse("edit_property", args=[draft.pk]))
        self.assertContains(response, "data-ai-bundle-trigger")
        self.assertContains(response, 'id="ai-inline-config"')

    def test_new_user_can_create_personal_demo_data(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)
        self.assertFalse(any(path.is_file() for path in self.media_root.rglob("*")))

        response = self.client.post(reverse("onboarding_demo_create"))

        self.assertRedirects(response, reverse("property_list"))
        demo_property = Property.objects.get(owner=user, is_demo=True)
        self.assertTrue(demo_property.landing_published)
        self.assertEqual(demo_property.images.count(), 3)
        self.assertIn("demo-demo-living-room", demo_property.images.get(is_primary=True).image.name)
        self.assertEqual(demo_property.leads.filter(is_demo=True).count(), 2)
        self.assertEqual(Client.objects.filter(owner=user, is_demo=True).count(), 2)
        self.assertEqual(Deal.objects.filter(owner=user, is_demo=True).count(), 1)
        self.assertEqual(Showing.objects.filter(owner=user, deal__is_demo=True).count(), 1)
        self.assertEqual(ClientReminder.objects.filter(owner=user, deal__is_demo=True).count(), 1)
        self.assertTrue(RealtorProfile.objects.get(user=user).demo_data_created)
        self.assertEqual(sum(path.is_file() for path in self.media_root.rglob("*")), 3)
        self.client.post(reverse("onboarding_demo_create"))
        self.assertEqual(sum(path.is_file() for path in self.media_root.rglob("*")), 3)

    def test_user_can_hide_the_first_steps_checklist(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)

        self.assertRedirects(self.client.post(reverse("onboarding_dismiss")), reverse("property_list"))
        response = self.client.get(reverse("property_list"))

        self.assertNotContains(response, "Первые 10 минут")

    def test_owner_can_edit_all_property_card_fields(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)
        response = self.client.post(
            reverse("edit_property", args=[property.pk]),
            {
                "title": "Дом у моря",
                "property_type": "house",
                "deal_type": "sale",
                "status": "published",
                "avito_operation": "sell",
                "price": "450000",
                "currency": "EUR",
                "address": "Лиссабон",
                "area": "180",
                "rooms": "5",
                "bathrooms": "3",
                "floor": "1",
                "floors_total": "2",
                "land_area": "600",
                "year_built": "2022",
                "condition": "excellent",
                "amenities": "Бассейн, парковка",
                "description": "Готовое описание",
                "landing_template": "classic",
                "landing_accent": "emerald",
                "landing_button_style": "rounded",
                "landing_hero_layout": "background",
                "landing_gallery_style": "grid",
                "landing_published": "on",
            },
        )
        self.assertRedirects(response, reverse("property_detail", args=[property.pk]))
        property.refresh_from_db()
        self.assertEqual(property.status, "published")
        self.assertEqual(property.land_area, 600)
        self.assertEqual(property.landing_accent, "emerald")
        self.assertEqual(property.landing_gallery_style, "grid")

    def test_property_accepts_missing_landing_settings_and_rejects_invalid_ones(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        valid_response = self.client.post(
            reverse("edit_property", args=[property.pk]),
            {
                "title": "Квартира у парка",
                "property_type": "apartment",
                "deal_type": "sale",
                "status": "draft",
                "avito_operation": "sell",
                "currency": "RUB",
                "landing_template": "classic",
            },
        )
        self.assertRedirects(valid_response, reverse("property_detail", args=[property.pk]))

        invalid_response = self.client.post(
            reverse("edit_property", args=[property.pk]),
            {
                "title": "Квартира у парка",
                "property_type": "apartment",
                "deal_type": "sale",
                "status": "draft",
                "avito_operation": "sell",
                "currency": "RUB",
                "landing_template": "classic",
                "landing_block_order": "{}",
                "landing_enabled_blocks": "[]",
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertIn("landing_block_order", invalid_response.context["form"].errors)
        self.assertIn("landing_enabled_blocks", invalid_response.context["form"].errors)

    def test_published_landing_accepts_a_lead(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(
            title="Публичный объект", owner=owner, landing_published=True
        )
        landing_url = reverse("public_landing", args=[property.landing_slug])
        self.assertEqual(self.client.get(landing_url).status_code, 200)
        response = self.client.post(
            landing_url,
            {"contact_purpose": "viewing", "name": "Мария", "phone": "+351900000000", "message": "Хочу посмотреть", "personal_data_consent": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(property.leads.count(), 1)
        self.assertEqual(property.leads.get().contact_purpose, "viewing")
        self.assertEqual(property.leads.get().personal_data_consent_version, "2026-09-21")

    @patch("properties.views.LeadNotificationService.notify_new_lead")
    def test_demo_landing_creates_a_demo_lead_without_notifications(self, notify_new_lead):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(
            title="Демо-объект", owner=owner, landing_published=True, is_demo=True
        )
        self.client.force_login(owner)

        response = self.client.post(
            reverse("public_landing", args=[property.landing_slug]),
            {
                "contact_purpose": "viewing",
                "name": "Тестовый клиент",
                "phone": "+70000000003",
                "personal_data_consent": "on",
            },
        )

        lead = property.leads.get()
        self.assertTrue(lead.is_demo)
        notify_new_lead.assert_not_called()
        self.assertContains(response, "Вы прошли путь от лендинга до обращения клиента")

    def test_honeypot_submission_does_not_create_a_lead(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)

        response = self.client.post(
            reverse("public_landing", args=[property.landing_slug]),
            {
                "contact_purpose": "viewing",
                "name": "Спам-бот",
                "phone": "+351900000000",
                "website": "https://spam.example.com",
                "personal_data_consent": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["sent"])
        self.assertEqual(property.leads.count(), 0)

    @override_settings(PUBLIC_LEAD_RATE_LIMIT=10, PUBLIC_LEAD_RATE_LIMIT_PER_PROPERTY=1)
    def test_public_landing_limits_submissions_for_one_object(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)
        landing_url = reverse("public_landing", args=[property.landing_slug])
        payload = {"contact_purpose": "viewing", "name": "Мария", "phone": "+351900000000", "personal_data_consent": "on"}

        self.assertEqual(self.client.post(landing_url, payload).status_code, 200)
        response = self.client.post(landing_url, payload)

        self.assertEqual(property.leads.count(), 1)
        self.assertContains(response, "Слишком много попыток")

    @override_settings(
        TURNSTILE_ENABLED=True,
        TURNSTILE_SECRET_KEY="test-secret",
        TURNSTILE_ALLOWED_HOSTNAMES={"testserver"},
    )
    @patch("properties.services.public_form_security.requests.post")
    def test_public_landing_validates_turnstile_before_creating_a_lead(self, post):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)
        verification_response = Mock()
        verification_response.json.return_value = {"success": False}
        post.return_value = verification_response

        response = self.client.post(
            reverse("public_landing", args=[property.landing_slug]),
            {
                "contact_purpose": "viewing",
                "name": "Мария",
                "phone": "+351900000000",
                "cf-turnstile-response": "invalid-token",
                "personal_data_consent": "on",
            },
        )

        self.assertEqual(property.leads.count(), 0)
        self.assertContains(response, "Не удалось подтвердить, что вы не робот")
        self.assertEqual(post.call_args.kwargs["data"]["secret"], "test-secret")

    @override_settings(TURNSTILE_ENABLED=True, TURNSTILE_SITE_KEY="site-key")
    def test_public_landing_renders_turnstile_when_enabled(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)

        response = self.client.get(reverse("public_landing", args=[property.landing_slug]))

        self.assertContains(response, "challenges.cloudflare.com/turnstile/v0/api.js")
        self.assertContains(response, 'data-sitekey="site-key"')

    @patch("properties.views.LeadNotificationService.notify_new_lead")
    def test_published_landing_schedules_a_realtor_notification(self, notify_new_lead):
        owner = User.objects.create_user("agent", password="password")
        RealtorProfile.objects.create(user=owner, email="agent@example.com")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("public_landing", args=[property.landing_slug]),
                {"contact_purpose": "viewing", "name": "Мария", "phone": "+351900000000", "personal_data_consent": "on"},
            )

        self.assertEqual(response.status_code, 200)
        notify_new_lead.assert_called_once()
        self.assertEqual(notify_new_lead.call_args.args[0].name, "Мария")
        self.assertEqual(notify_new_lead.call_args.args[1].email, "agent@example.com")

    def test_all_landing_templates_render(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)
        landing_url = reverse("public_landing", args=[property.landing_slug])

        for template, _label in Property.LANDING_TEMPLATES:
            property.landing_template = template
            property.save(update_fields=["landing_template", "updated_at"])
            response = self.client.get(landing_url)
            self.assertEqual(response.status_code, 200, template)
            self.assertContains(response, "Записаться на просмотр")
            self.assertContains(response, reverse("privacy_policy"))

    def test_public_landing_requires_personal_data_consent(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Публичный объект", owner=owner, landing_published=True)

        response = self.client.post(
            reverse("public_landing", args=[property.landing_slug]),
            {"contact_purpose": "viewing", "name": "Мария", "phone": "+351900000000"},
        )

        self.assertEqual(property.leads.count(), 0)
        self.assertContains(response, "необходимо согласие на обработку персональных данных")

    def test_legal_documents_are_publicly_available(self):
        for url_name in ("privacy_policy", "personal_data_consent", "service_terms"):
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, url_name)
            self.assertContains(response, 'class="legal-page"')
            self.assertContains(response, 'class="legal-nav"')

    def test_export_pages_share_ready_and_empty_states(self):
        owner = User.objects.create_user("export-owner", password="password")
        self.client.force_login(owner)
        for url_name in ("avito_export", "cian_export"):
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, url_name)
            self.assertContains(response, 'class="export-summary"')
            self.assertContains(response, 'class="app-empty-state export-empty"')

    def test_landing_visual_settings_render_the_selected_variants(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(
            title="Публичный объект",
            owner=owner,
            landing_published=True,
            landing_accent="emerald",
            landing_button_style="strict",
            landing_hero_layout="background",
            landing_gallery_style="grid",
        )
        PropertyImage.objects.create(property=property, image=self.image_upload(), is_primary=True)
        PropertyImage.objects.create(property=property, image=self.image_upload(), order=1)

        response = self.client.get(reverse("public_landing", args=[property.landing_slug]))

        self.assertContains(response, "landing-accent-emerald")
        self.assertContains(response, "landing-buttons-strict")
        self.assertContains(response, "landing-hero-background")
        self.assertContains(response, 'class="gallery-grid"')

    def test_owner_can_download_a_property_presentation_pdf(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Дом у парка", owner=owner, price="4500000", area=120)
        self.client.force_login(owner)

        response = self.client.get(
            reverse("property_presentation_pdf", args=[property.pk]),
            {"facts": "0", "description": "0", "benefits": "0", "contacts": "0", "photos": "0", "template": "premium"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_owner_can_download_an_editable_property_presentation_docx(self):
        owner = User.objects.create_user("agent-docx", password="password")
        property = Property.objects.create(title="Квартира у парка", owner=owner, price="4200000")
        self.client.force_login(owner)

        response = self.client.get(reverse("property_presentation_docx", args=[property.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertTrue(response.content.startswith(b"PK"))

    def test_owner_can_upload_property_image(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.post(
            reverse("upload_images", args=[property.pk]),
            {"images": self.image_upload()},
        )

        self.assertRedirects(response, reverse("property_detail", args=[property.pk]))
        self.assertEqual(property.images.count(), 1)
        self.assertTrue(property.images.get().image.name.endswith(".jpg"))
        self.assertTrue(property.images.get().is_primary)

    def test_property_readiness_lists_all_seven_requirements(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("property_detail", args=[property.pk]))

        self.assertEqual(response.context["readiness_total"], 7)
        self.assertEqual(response.context["readiness_completed"], 0)
        self.assertContains(response, "Следующий шаг: <a")
        self.assertNotContains(response, "Начните с цены, адреса и главной фотографии")
        photo_tasks = [task for task in response.context["readiness_tasks"] if "фото" in task["text"]]
        self.assertEqual(photo_tasks, [
            {"text": "Добавьте главное фото", "url": reverse("upload_images", args=[property.pk])},
            {"text": "Добавьте ещё 3 фото", "url": reverse("upload_images", args=[property.pk])},
        ])
        self.assertContains(response, "Укажите контакты риелтора")

    def test_property_readiness_opens_photo_tab_when_primary_is_missing(self):
        owner = User.objects.create_user("photo-owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        PropertyImage.objects.create(property=property, image=self.image_upload(), is_primary=False)
        self.client.force_login(owner)

        response = self.client.get(reverse("property_detail", args=[property.pk]))
        photo_tasks = [task for task in response.context["readiness_tasks"] if "фото" in task["text"]]

        self.assertEqual(photo_tasks[0], {
            "text": "Назначьте главную фотографию",
            "url": f"{reverse('property_detail', args=[property.pk])}#photos-pane",
        })
        self.assertEqual(photo_tasks[1]["url"], reverse("upload_images", args=[property.pk]))
        self.assertContains(response, "window.addEventListener('hashchange', showTabFromHash)")

    def test_property_detail_renders_preview_presentation_and_archive_action(self):
        owner = User.objects.create_user("detail-owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("property_detail", args=[property.pk]))

        self.assertContains(response, 'id="property-workspace-tabs"')
        self.assertContains(response, 'id="gallery-card"')
        self.assertContains(response, 'data-preview-frame')
        self.assertContains(response, reverse("property_landing_preview", args=[property.pk]))
        self.assertContains(response, reverse("property_presentation_pdf", args=[property.pk]))
        self.assertContains(response, reverse("property_presentation_docx", args=[property.pk]))
        self.assertContains(response, 'data-confirm-title="Архивировать объект?"')
        self.assertContains(response, 'aria-label="Пояснение: Архив объекта"')
        self.assertContains(response, 'class="ui-help-content" hidden>Уберите объект из рабочего списка')
        self.assertNotContains(response, '<p class="detail-muted">Уберите объект из рабочего списка')
        self.assertNotContains(response, "Восстановить объект нельзя")

    def test_lead_detail_keeps_client_and_property_navigation(self):
        owner = User.objects.create_user("detail-owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        lead = Lead.objects.create(property=property, client=client, name="Анна", phone=client.phone, message="Хочу посмотреть")
        self.client.force_login(owner)

        response = self.client.get(reverse("lead_detail", args=[lead.pk]))

        self.assertContains(response, 'id="lead-overview" role="tabpanel"')
        self.assertContains(response, 'id="lead-about" role="tabpanel"')
        self.assertContains(response, 'id="lead-history" role="tabpanel"')
        self.assertContains(response, "Хочу посмотреть")
        self.assertContains(response, reverse("client_detail", args=[client.pk]))
        self.assertContains(response, reverse("property_detail", args=[property.pk]))
        self.assertContains(response, 'data-confirm-title="Архивировать заявку?"')

    def test_client_detail_shows_next_reminder_and_history_entry_stays_on_tab(self):
        owner = User.objects.create_user("detail-owner", password="password")
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        ClientReminder.objects.create(client=client, text="Позвонить завтра", due_at=timezone.now() + timedelta(days=1))
        self.client.force_login(owner)

        response = self.client.get(reverse("client_detail", args=[client.pk]))

        self.assertContains(response, 'class="detail-summary-bar client-detail-summary"')
        self.assertContains(response, 'aria-label="Показать следующие вкладки"')
        self.assertContains(response, "Позвонить завтра")
        self.assertEqual(response.content.decode().count("Позвонить завтра"), 1)
        self.assertEqual(len(response.context["reminders"]), 0)
        self.assertContains(response, f'id="client-status-{client.pk}"')
        self.assertContains(response, 'class="detail-status-control__editor" x-data="{ changed: false }"')
        self.assertContains(response, 'x-show="changed" x-cloak>Сохранить</button>')
        self.assertContains(response, 'id="client-history"')

        interaction_response = self.client.post(reverse("client_interaction_create", args=[client.pk]), {
            "interaction_type": "note", "text": "Обсудили условия",
        })
        self.assertRedirects(interaction_response, f"{reverse('client_detail', args=[client.pk])}#client-history", fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("client_detail", args=[client.pk])), "Обсудили условия")

    def test_client_detail_shows_unscheduled_task_as_main_step(self):
        owner = User.objects.create_user("unscheduled-owner", password="password")
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        task = ClientReminder.objects.create(owner=owner, client=client, text="Уточнить интерес")
        self.client.force_login(owner)

        response = self.client.get(reverse("client_detail", args=[client.pk]))

        self.assertContains(response, "Уточнить интерес")
        self.assertContains(response, "Без срока")
        self.assertEqual([item.pk for item in response.context["reminders"]], [])
        self.assertEqual(response.context["next_reminder"].pk, task.pk)

    def test_client_status_quick_action_keeps_htmx_control(self):
        owner = User.objects.create_user("detail-owner", password="password")
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        self.client.force_login(owner)

        response = self.client.post(reverse("client_status_update", args=[client.pk]),
                                    {"status": "in_progress"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="client-status-{client.pk}"')
        self.assertContains(response, 'class="detail-status-control"')
        self.assertContains(response, 'aria-label="Изменить отношения с клиентом: В работе"')
        self.assertContains(response, 'x-show="changed" x-cloak>Сохранить</button>')
        client.refresh_from_db()
        self.assertEqual(client.status, "in_progress")

    def test_legacy_ai_history_routes_redirect_to_inline_text_editor(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        ai_content = AIContent.objects.create(
            property=property, content_type="headline", tone="business", title=property.title, content="Текст"
        )
        self.client.force_login(owner)
        editor_url = f"{reverse('edit_property', args=[property.pk])}#texts-pane"

        self.assertRedirects(self.client.get(reverse("ai_assistant", args=[property.pk])), editor_url)
        self.assertRedirects(self.client.get(reverse("ai_content_edit", args=[ai_content.pk])), editor_url)
        self.assertRedirects(self.client.post(reverse("ai_content_delete", args=[ai_content.pk])), editor_url)
        self.assertTrue(AIContent.objects.filter(pk=ai_content.pk).exists())

    def test_legacy_ai_templates_render_with_shared_panels(self):
        owner = User.objects.create_user("ai-owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        ai_content = AIContent.objects.create(
            property=property, content_type="headline", tone="business", title=property.title, content="Текст"
        )
        request = RequestFactory().get(reverse("property_list"))
        request.user = owner
        request.resolver_match = resolve(reverse("property_list"))
        history_context = get_ai_history_context(property)

        assistant_html = render_to_string("properties/ai_assistant.html", {
            "property": property, "form": AIRequestForm(), **history_context,
        }, request=request)
        editor_html = render_to_string("properties/ai_content_edit.html", {
            "ai_content": ai_content, "form": AIContentEditForm(instance=ai_content),
        }, request=request)

        self.assertIn('class="ai-workspace"', assistant_html)
        self.assertIn('class="ai-panel ai-history"', assistant_html)
        self.assertIn('class="ai-panel ai-content-editor"', editor_html)

    @patch("properties.ai_views.AIService.generate_inline_content")
    def test_inline_ai_returns_text_without_overwriting_property(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner, description="Ручной текст")
        generate_inline_content.return_value = (
            "full_description",
            {"content": "Текст от ИИ", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1},
        )
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "business", "mode": "generate"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "Текст от ИИ")
        property.refresh_from_db()
        self.assertEqual(property.description, "Ручной текст")

    def test_agent_cannot_generate_inline_text_for_another_property(self):
        owner = User.objects.create_user("owner", password="password")
        visitor = User.objects.create_user("visitor", password="password")
        property = Property.objects.create(title="Закрытый объект", owner=owner)
        self.client.force_login(visitor)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "business", "mode": "generate"},
        )

        self.assertEqual(response.status_code, 404)

    @patch("properties.ai_views.AIService.generate_bundle")
    def test_ai_bundle_returns_preview_without_overwriting_property(self, generate_bundle):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner, short_description="Ручной текст")
        base_result = {"prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1}
        generate_bundle.return_value = [
            {**base_result, "content_type": "headline", "content": "Заголовок"},
            {**base_result, "content_type": "short_description", "content": "Короткий текст"},
            {**base_result, "content_type": "full_description", "content": "Полный текст"},
        ]
        self.client.force_login(owner)

        response = self.client.post(reverse("ai_bundle_generate", args=[property.pk]), {"tone": "business"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["contents"]), 3)
        property.refresh_from_db()
        self.assertEqual(property.short_description, "Ручной текст")

    @patch("properties.services.ai_service.AIService._run_prompt")
    def test_full_bundle_generates_distinct_ad_and_landing_fields_in_one_request(self, run_prompt):
        owner = User.objects.create_user("bundle-owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner, description="Ручной текст")
        types = (
            "headline", "short_description", "full_description", "landing_headline", "landing_subtitle",
            "seo_title", "seo_description", "benefits", "cta",
        )
        run_prompt.return_value = {
            "content": "\n".join(f"[[{kind}]] Текст {kind}" for kind in types),
            "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1,
        }
        self.client.force_login(owner)

        response = self.client.post(reverse("ai_bundle_generate", args=[property.pk]), {"tone": "business"})

        self.assertEqual(response.status_code, 200)
        targets = [item["target"] for item in response.json()["contents"]]
        self.assertEqual(len(targets), 9)
        self.assertEqual(len(set(targets)), 9)
        self.assertIn("description", targets)
        self.assertIn("landing_title", targets)
        self.assertIn("landing_trust_about", targets)
        self.assertIn("seo_description", targets)
        self.assertEqual(run_prompt.call_count, 1)
        self.assertIn("Возможности сервиса:", run_prompt.call_args.args[0])
        self.assertEqual(AIContent.objects.filter(property=property).count(), 9)
        property.refresh_from_db()
        self.assertEqual(property.description, "Ручной текст")

    @patch("properties.services.ai_service.AIService._run_prompt")
    def test_realtor_trust_prompt_uses_service_facts(self, run_prompt):
        property = Property(title="Квартира", area=58, rooms=2)
        run_prompt.return_value = {"content": "Ответим на вопросы и согласуем просмотр."}

        kind, _ = AIService.generate_inline_content(property, "landing_trust_about", "business")

        self.assertEqual(kind, "benefits")
        prompt = run_prompt.call_args.args[0]
        self.assertIn("Возможности сервиса:", prompt)
        self.assertIn("Не перечисляй характеристики объекта", prompt)

    @patch("properties.ai_views.AIService.generate_bundle")
    def test_ai_landing_bundle_uses_landing_scenario(self, generate_bundle):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        generate_bundle.return_value = [{
            "content_type": "landing_headline", "content": "Заголовок лендинга", "prompt": "test",
            "provider": "ollama", "model": "test", "duration_ms": 1,
        }]
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_bundle_generate", args=[property.pk]), {"tone": "premium", "bundle": "landing"}
        )

        self.assertEqual(response.status_code, 200)
        generate_bundle.assert_called_once_with(property, "premium", "landing")
        self.assertEqual(response.json()["contents"][0]["target"], "landing_title")

    @patch("properties.ai_views.AIService.generate_inline_content")
    def test_all_inline_ai_text_targets_are_available(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        generate_inline_content.return_value = (
            "benefits",
            {"content": "Текст от ИИ", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1},
        )
        self.client.force_login(owner)
        targets = [
            "marketing_headline", "short_description", "description", "landing_title", "landing_subtitle",
            "landing_about_title", "landing_contact_title", "landing_trust_about", "seo_title", "seo_description",
            "landing_benefit_1_title", "landing_benefit_1_description",
        ]

        for target in targets:
            response = self.client.post(
                reverse("ai_inline_generate", args=[property.pk]),
                {"target": target, "tone": "emotional", "mode": "generate"},
            )
            self.assertEqual(response.status_code, 200, target)

    @patch("properties.ai_views.AIService.generate_inline_content")
    def test_inline_ai_accepts_all_tones(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        generate_inline_content.return_value = (
            "full_description",
            {"content": "Текст от ИИ", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1},
        )
        self.client.force_login(owner)

        for tone in ("business", "premium", "concise", "emotional"):
            response = self.client.post(
                reverse("ai_inline_generate", args=[property.pk]),
                {"target": "description", "tone": tone, "mode": "generate"},
            )
            self.assertEqual(response.status_code, 200, tone)

    @patch("properties.ai_views.AIService.improve_text")
    def test_inline_ai_improves_manual_text_without_saving_property(self, improve_text):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner, description="Ручной текст")
        improve_text.return_value = {"content": "Улучшенный текст", "prompt": "test", "provider": "ollama", "model": "test", "duration_ms": 1}
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "premium", "mode": "improve", "improvement": "premium", "source_text": "Ручной текст"},
        )

        self.assertEqual(response.status_code, 200)
        improve_text.assert_called_once_with(property, "full_description", "premium", "premium", "Ручной текст")
        property.refresh_from_db()
        self.assertEqual(property.description, "Ручной текст")

    @patch("properties.ai_views.AIService.generate_inline_content", side_effect=AIServiceError("Ollama недоступна"))
    def test_inline_ai_returns_provider_error(self, generate_inline_content):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.post(
            reverse("ai_inline_generate", args=[property.pk]),
            {"target": "description", "tone": "business", "mode": "generate"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Ollama недоступна")

    @override_settings(AI_ENABLED=False)
    def test_ai_pages_are_unavailable_when_disabled(self):
        owner = User.objects.create_user("owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("ai_assistant", args=[property.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(
            self.client.get(reverse("property_detail", args=[property.pk])),
            "ИИ-помощник",
        )
        self.assertNotContains(
            self.client.get(reverse("edit_property", args=[property.pk])),
            "Создать комплект текстов",
        )
        self.assertEqual(
            self.client.post(reverse("ai_inline_generate", args=[property.pk]), {"target": "description"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("ai_bundle_generate", args=[property.pk]), {"tone": "business"}).status_code,
            404,
        )


class DealWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("deal-owner", password="password")
        self.other = User.objects.create_user("other-deal-owner", password="password")
        self.client_record = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000010")
        self.property = Property.objects.create(owner=self.owner, title="Квартира у парка")
        self.lead = Lead.objects.create(
            property=self.property, client=self.client_record, name="Анна", phone=self.client_record.phone,
        )
        self.lead.qualification = "qualified"
        self.lead.first_contacted_at = timezone.now()
        self.lead.save(update_fields=["qualification", "first_contacted_at"])
        self.client.force_login(self.owner)

    def payload(self, **overrides):
        return {
            "client": self.client_record.pk,
            "property": self.property.pk,
            "lead": self.lead.pk,
            "stage": "new",
            "expected_commission": "120000.00",
            "outcome_note": "",
            **overrides,
        }

    def test_create_deal_from_lead_and_show_on_related_pages(self):
        lead_task = ClientReminder.objects.get(lead=self.lead, is_done=False)
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), "Перевести в сделку")
        form_page = self.client.get(f"{reverse('deal_create')}?lead={self.lead.pk}")
        self.assertEqual(form_page.context["form"].initial["client"], self.client_record)
        self.assertNotContains(form_page, 'id="deal-tabs"')
        self.assertEqual(form_page.context["form"].initial["property"], self.property)
        self.assertEqual(form_page.context["form"].initial["responsible"], self.owner)
        response = self.client.post(reverse("deal_create"), self.payload())

        deal = Deal.objects.get()
        self.assertRedirects(response, reverse("deal_detail", args=[deal.pk]))
        self.assertEqual((deal.owner, deal.responsible, deal.client, deal.property, deal.lead),
                         (self.owner, self.owner, self.client_record, self.property, self.lead))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.outcome, "converted")
        self.assertEqual(self.lead.outcome_deal, deal)
        self.assertIsNotNone(self.lead.outcome_at)
        self.assertFalse(ClientReminder.objects.filter(lead=self.lead, is_done=False).exists())
        deal_task = ClientReminder.objects.get(deal=deal, is_done=False)
        self.assertEqual((deal_task.text, deal_task.due_at, deal_task.owner), (lead_task.text, lead_task.due_at, self.owner))
        self.assertEqual(str(deal.expected_commission), "120000.00")
        self.assertContains(self.client.get(reverse("client_detail", args=[self.client_record.pk])), reverse("deal_detail", args=[deal.pk]))
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), reverse("deal_detail", args=[deal.pk]))

        repeated = self.client.post(reverse("deal_create"), self.payload())
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(Deal.objects.count(), 1)
        self.assertEqual(ClientReminder.objects.filter(deal=deal, is_done=False).count(), 1)

    def test_unqualified_lead_cannot_be_converted(self):
        self.lead.qualification = "pending"
        self.lead.save(update_fields=["qualification"])
        response = self.client.post(reverse("deal_create"), self.payload())
        self.assertContains(response, "Сначала зафиксируйте первый контакт")
        self.assertFalse(Deal.objects.exists())
        self.assertEqual(self.lead.outcome, "open")

    def test_one_client_can_have_independent_deals_for_different_properties(self):
        second_property = Property.objects.create(owner=self.owner, title="Дом у леса")
        Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property)
        Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=second_property, stage="viewing")
        Deal.objects.create(
            owner=self.other,
            responsible=self.other,
            client=Client.objects.create(owner=self.other, name="Чужой клиент", phone="+79990000011"),
            property=Property.objects.create(owner=self.other, title="Чужой объект"),
        )

        response = self.client.get(reverse("deal_board"))

        self.assertContains(response, "Квартира у парка")
        self.assertContains(response, "Дом у леса")
        self.assertNotContains(response, "Чужой клиент")
        self.assertEqual(response.context["active_count"], 2)
        self.assertEqual(self.client_record.status, "new")

    def test_deal_board_pages_cards_and_keeps_next_work_visible(self):
        now = timezone.now()
        for index in range(13):
            property_record = Property.objects.create(owner=self.owner, title=f"Объект {index:02d}")
            deal = Deal.objects.create(
                owner=self.owner, responsible=self.owner, client=self.client_record, property=property_record,
            )
            if index == 12:
                Showing.objects.create(owner=self.owner, deal=deal, starts_at=now - timedelta(days=1))
                future = Showing.objects.create(owner=self.owner, deal=deal, starts_at=now + timedelta(days=1))
                ClientReminder.objects.create(owner=self.owner, deal=deal, text="Согласовать показ", due_at=now + timedelta(hours=2))

        first = self.client.get(reverse("deal_board"))
        first_new = next(column for column in first.context["columns"] if column["key"] == "new")
        self.assertEqual((first_new["count"], len(first_new["page"]), first.context["active_count"]), (13, 12, 13))
        self.assertEqual(first_new["page"][0].next_showing_at, future.starts_at)
        self.assertEqual(first_new["page"][0].next_task_text, "Согласовать показ")
        self.assertContains(first, "Согласовать показ")
        self.assertNotContains(first, "Объект 00")

        second = self.client.get(first_new["next_url"])
        second_new = next(column for column in second.context["columns"] if column["key"] == "new")
        self.assertEqual((second_new["count"], len(second_new["page"])), (13, 1))
        self.assertContains(second, "Объект 00")

    def test_active_duplicate_is_rejected_but_new_deal_after_closing_is_allowed(self):
        existing = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property)
        response = self.client.post(reverse("deal_create"), self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "уже есть активная сделка")
        self.assertEqual(Deal.objects.count(), 1)

        response = self.client.post(reverse("deal_detail", args=[existing.pk]), self.payload(stage="won", outcome_note="Сделка завершена"))
        self.assertRedirects(response, reverse("deal_detail", args=[existing.pk]))
        existing.refresh_from_db()
        self.assertIsNotNone(existing.closed_at)

        response = self.client.post(reverse("deal_create"), self.payload(lead=""))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Deal.objects.count(), 2)

    def test_foreign_client_property_and_deal_are_not_accessible(self):
        foreign_client = Client.objects.create(owner=self.other, name="Чужой клиент", phone="+79990000012")
        foreign_property = Property.objects.create(owner=self.other, title="Чужой объект")
        foreign_lead = Lead.objects.create(property=foreign_property, client=foreign_client, name="Чужой клиент", phone=foreign_client.phone)
        foreign_deal = Deal.objects.create(owner=self.other, responsible=self.other, client=foreign_client, property=foreign_property)

        self.assertEqual(self.client.get(reverse("deal_detail", args=[foreign_deal.pk])).status_code, 404)
        self.assertEqual(self.client.get(f"{reverse('deal_create')}?lead={foreign_lead.pk}").status_code, 404)
        response = self.client.post(reverse("deal_create"), self.payload(client=foreign_client.pk, property=foreign_property.pk, lead=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Deal.objects.filter(owner=self.owner).exists())

    def test_deal_pair_cannot_be_changed_through_edit_form(self):
        second_property = Property.objects.create(owner=self.owner, title="Другая квартира")
        deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property, lead=self.lead)

        response = self.client.post(reverse("deal_detail", args=[deal.pk]), self.payload(property=second_property.pk, stage="viewing"))

        self.assertRedirects(response, reverse("deal_detail", args=[deal.pk]))
        deal.refresh_from_db()
        self.assertEqual(deal.property, self.property)
        self.assertEqual(deal.stage, "viewing")

    def test_edit_separates_deal_overview_from_tasks_and_showings(self):
        deal = Deal.objects.create(
            owner=self.owner, responsible=self.owner,
            client=self.client_record, property=self.property, lead=self.lead,
        )
        url = reverse("deal_detail", args=[deal.pk])
        response = self.client.get(url)
        page = response.content.decode()

        self.assertContains(response, reverse("client_detail", args=[self.client_record.pk]))
        self.assertContains(response, reverse("property_detail", args=[self.property.pk]))
        self.assertNotIn('name="client"', page)
        self.assertNotIn('name="property"', page)
        self.assertContains(response, 'id="deal-overview-tab"')
        self.assertContains(response, 'id="deal-work-tab"')
        self.assertLess(page.index('class="deal-focus"'), page.index('id="deal-tabs"'))
        self.assertContains(response, 'class="detail-status-control__editor deal-focus__editor"')
        self.assertContains(response, 'aria-label="Изменить этап сделки: Новая"')
        self.assertContains(response, '<use href="#ui-icon-edit"></use>')
        self.assertNotContains(response, 'class="deal-focus__edit-label"')
        self.assertLess(page.index('id="deal-overview" role="tabpanel"'), page.index('aria-label="Связанные данные сделки"'))
        self.assertLess(page.index('id="id_expected_commission"'), page.index('id="deal-work" role="tabpanel"'))
        self.assertLess(page.index('id="deal-work" role="tabpanel"'), page.index('id="deal-tasks-title"'))
        self.assertLess(page.index('id="deal-work" role="tabpanel"'), page.index('id="deal-showings-title"'))

        save = self.client.post(url, {"stage": "viewing", "expected_commission": "120000.00"})
        self.assertRedirects(save, url)
        deal.refresh_from_db()
        self.assertEqual((deal.client, deal.property, deal.stage), (self.client_record, self.property, "viewing"))

    def test_client_summary_uses_deal_task_when_no_other_reminder_exists(self):
        deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property)
        ClientReminder.objects.create(
            owner=self.owner, client=self.client_record, deal=deal,
            text="Подтвердить время показа", due_at=timezone.now() + timedelta(days=1),
        )

        response = self.client.get(reverse("client_detail", args=[self.client_record.pk]))

        self.assertContains(response, "Подтвердить время показа")
        self.assertNotContains(response, "Запланируйте контакт с клиентом")

    def test_repeated_lead_opens_existing_active_deal_for_same_pair(self):
        deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property, lead=self.lead)
        later_lead = Lead.objects.create(property=self.property, client=self.client_record, name="Анна", phone=self.client_record.phone)

        response = self.client.get(reverse("lead_detail", args=[later_lead.pk]))

        self.assertContains(response, reverse("deal_detail", args=[deal.pk]))
        self.assertNotContains(response, f"{reverse('deal_create')}?lead={later_lead.pk}")

    def test_lost_deal_requires_result(self):
        response = self.client.post(reverse("deal_create"), self.payload(stage="lost", outcome_note=""))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Укажите причину отказа")
        self.assertFalse(Deal.objects.exists())


class WorkItemsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("items-owner", password="password")
        self.other = User.objects.create_user("items-other", password="password")
        self.client_record = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000031")
        self.property = Property.objects.create(owner=self.owner, title="Квартира у парка")
        self.deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property)
        self.client.force_login(self.owner)

    def test_all_items_shows_open_work_in_deadline_order_without_duplicate_lead_task(self):
        now = timezone.localtime(timezone.now()).replace(hour=12, minute=0, second=0, microsecond=0)
        ClientReminder.objects.create(owner=self.owner, text="Просроченный звонок", due_at=now - timedelta(hours=2))
        ClientReminder.objects.create(owner=self.owner, text="Звонок сегодня", due_at=now + timedelta(minutes=5))
        ClientReminder.objects.create(owner=self.owner, text="Задача на завтра", due_at=now + timedelta(days=2))
        ClientReminder.objects.create(owner=self.owner, text="Без даты")
        ClientReminder.objects.create(owner=self.owner, text="Выполнено", is_done=True)
        ClientReminder.objects.create(owner=self.owner, text="Отменено", cancelled_at=now)
        ClientReminder.objects.create(owner=self.other, text="Чужая задача")
        lead = Lead.objects.create(
            property=self.property, name="Клиент по заявке", phone="+79990000032",
            first_contacted_at=now, next_action="Уточнить условия", next_action_due_at=now + timedelta(days=1),
        )
        showing = Showing.objects.create(owner=self.owner, deal=self.deal, starts_at=now + timedelta(days=3))
        Showing.objects.create(owner=self.owner, deal=self.deal, starts_at=now + timedelta(days=4), status="completed", outcome_note="Проведён")
        other_client = Client.objects.create(owner=self.other, name="Чужой клиент", phone="+79990000035")
        other_property = Property.objects.create(owner=self.other, title="Чужой объект")
        other_deal = Deal.objects.create(owner=self.other, client=other_client, property=other_property)
        Lead.objects.create(property=other_property, name="Чужая заявка", phone="+79990000036")
        Showing.objects.create(owner=self.other, deal=other_deal, starts_at=now + timedelta(days=2))

        with patch("properties.workday_views.timezone.now", return_value=now):
            response = self.client.get(reverse("work_items"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([group["title"] for group in response.context["groups"]], ["Просрочено", "Сегодня", "Позже", "Без срока"])
        items = [item for group in response.context["groups"] for item in group["items"]]
        self.assertEqual(len(items), 6)
        self.assertEqual([(item["kind"], item["pk"]) for item in items if item["kind"] == "lead"], [("lead", lead.pk)])
        self.assertEqual([(item["kind"], item["pk"]) for item in items if item["kind"] == "showing"], [("showing", showing.pk)])
        self.assertEqual(items[0]["title"], "Просроченный звонок")
        self.assertEqual(items[-1]["title"], "Без даты")
        self.assertNotContains(response, "Чужая задача")
        self.assertNotContains(response, "Чужая заявка")
        self.assertNotContains(response, "Чужой объект")
        self.assertNotContains(response, "Выполнено")
        self.assertNotContains(response, "Отменено")
        self.assertContains(response, f'href="{reverse("lead_detail", args=[lead.pk])}"')
        self.assertContains(response, 'data-tooltip="Открыть заявку"')
        self.assertContains(response, 'data-tooltip="Открыть показ"')
        self.assertContains(response, 'aria-label="Период дел"')
        self.assertContains(response, 'aria-label="Тип дела"')
        self.assertContains(response, f'href="{reverse("work_items")}" aria-current="page"')

    def test_first_response_deadline_is_used_and_closed_leads_are_hidden(self):
        now = timezone.now()
        lead = Lead.objects.create(
            property=self.property, name="Новая заявка", phone="+79990000033",
            next_action="Позвонить позже", next_action_due_at=now + timedelta(days=2),
        )
        closed = Lead.objects.create(property=self.property, name="Закрытая заявка", phone="+79990000034")
        closed.status = "archived"
        closed.save(update_fields=["status"])

        response = self.client.get(reverse("work_items"), {"kind": "lead"})

        items = [item for group in response.context["groups"] for item in group["items"]]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["pk"], lead.pk)
        self.assertEqual(items[0]["title"], "Ответить клиенту")
        self.assertEqual(items[0]["due_at"], lead.first_response_due_at)

    def test_type_filter_and_pagination_keep_future_tasks_visible(self):
        now = timezone.now()
        for index in range(26):
            ClientReminder.objects.create(owner=self.owner, text=f"Дело {index:02d}", due_at=now + timedelta(days=2, minutes=index))
        Showing.objects.create(owner=self.owner, deal=self.deal, starts_at=now + timedelta(days=3))

        first = self.client.get(reverse("work_items"), {"kind": "task"})
        second = self.client.get(reverse("work_items"), {"kind": "task", "page": "2"})
        showings = self.client.get(reverse("work_items"), {"kind": "showing"})

        self.assertEqual(first.context["page_obj"].paginator.count, 26)
        self.assertContains(first, "Дело 00")
        self.assertNotContains(first, "Дело 25")
        self.assertContains(first, f"?next={reverse('work_items')}%3Fkind%3Dtask%26page%3D1")
        self.assertContains(second, "Дело 25")
        self.assertContains(second, "?kind=task&amp;page=1")
        self.assertEqual(showings.context["page_obj"].paginator.count, 1)
        self.assertEqual(showings.context["groups"][0]["items"][0]["kind"], "showing")

    def test_mixed_work_is_paged_after_merging_deadlines(self):
        now = timezone.now()
        for index in range(24):
            ClientReminder.objects.create(
                owner=self.owner, text=f"Звонок {index:02d}", due_at=now + timedelta(minutes=index + 1),
            )
        lead = Lead.objects.create(
            property=self.property, name="Новый клиент", phone="+79990000991",
            first_contacted_at=now, next_action="Отправить подборку",
            next_action_due_at=now + timedelta(minutes=25),
        )
        showing = Showing.objects.create(owner=self.owner, deal=self.deal, starts_at=now + timedelta(minutes=26))

        first = self.client.get(reverse("work_items"))
        second = self.client.get(reverse("work_items"), {"page": 2})
        first_items = [item for group in first.context["groups"] for item in group["items"]]
        second_items = [item for group in second.context["groups"] for item in group["items"]]

        self.assertEqual(first.context["page_obj"].paginator.count, 26)
        self.assertEqual((len(first_items), first_items[-1]["kind"], first_items[-1]["pk"]), (25, "lead", lead.pk))
        self.assertEqual([(item["kind"], item["pk"]) for item in second_items], [("showing", showing.pk)])

    def test_task_form_and_completion_return_to_all_items(self):
        task = ClientReminder.objects.create(owner=self.owner, text="Позвонить", due_at=timezone.now() + timedelta(days=1))
        target = f"{reverse('work_items')}?kind=task&page=1"
        form = self.client.get(reverse("task_detail", args=[task.pk]), {"next": target})
        self.assertContains(form, f'name="next" value="{reverse("work_items")}?kind=task&amp;page=1"')

        response = self.client.post(reverse("task_detail", args=[task.pk]), {
            "text": "Позвонить позже",
            "due_at": timezone.localtime(timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
            "client": "", "deal": "", "next": target,
        })
        self.assertRedirects(response, target)
        response = self.client.post(reverse("task_complete", args=[task.pk]), {"next": target})
        self.assertRedirects(response, target)
        self.assertFalse(any(item["kind"] == "task" for group in self.client.get(target).context["groups"] for item in group["items"]))

    def test_showing_form_returns_to_all_items(self):
        start = timezone.localtime(timezone.now() + timedelta(days=2))
        showing = Showing.objects.create(owner=self.owner, deal=self.deal, starts_at=start)
        target = reverse("work_items")
        form = self.client.get(reverse("showing_detail", args=[showing.pk]), {"next": target})
        self.assertContains(form, f'name="next" value="{target}"')

        response = self.client.post(reverse("showing_detail", args=[showing.pk]), {
            "deal": self.deal.pk, "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": "", "location": "", "status": "planned", "outcome_note": "", "next": target,
        })
        self.assertRedirects(response, target)


class WorkdayTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("workday-owner", password="password")
        self.other = User.objects.create_user("workday-other", password="password")
        self.client_record = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000021")
        self.property = Property.objects.create(owner=self.owner, title="Квартира")
        self.deal = Deal.objects.create(owner=self.owner, responsible=self.owner, client=self.client_record, property=self.property)
        self.client.force_login(self.owner)

    def test_workday_shows_overdue_tasks_and_today_showings_only_for_owner(self):
        overdue = ClientReminder.objects.create(
            client=self.client_record, deal=self.deal, text="Позвонить Анне", due_at=timezone.now() - timedelta(days=1),
        )
        local_today = timezone.localdate()
        fixed_now = timezone.make_aware(datetime.combine(local_today, time(12, 0)))
        starts_at = fixed_now + timedelta(hours=3)
        Showing.objects.create(owner=self.owner, deal=self.deal, starts_at=starts_at)
        other_client = Client.objects.create(owner=self.other, name="Чужой клиент", phone="+79990000022")
        ClientReminder.objects.create(client=other_client, text="Чужая задача", due_at=timezone.now() - timedelta(days=1))

        with patch("properties.workday_views.timezone.now", return_value=fixed_now):
            response = self.client.get(reverse("workday"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Позвонить Анне")
        self.assertContains(response, "Предстоящие показы")
        self.assertNotContains(response, "Чужая задача")
        self.assertEqual(response.context["overdue_tasks"].count(), 1)
        self.assertEqual(response.context["today_showings"].count(), 1)
        self.assertContains(response, f"{reverse('task_complete', args=[overdue.pk])}")
        self.assertEqual(response.context["nav_overdue_count"], 1)

    def test_general_task_can_be_created_completed_and_rescheduled(self):
        due_at = timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(reverse("task_create"), {"text": "Подготовить документы", "due_at": due_at, "client": "", "deal": ""})
        self.assertRedirects(response, reverse("workday"))
        task = ClientReminder.objects.get(text="Подготовить документы")
        self.assertEqual(task.owner, self.owner)
        self.assertIsNone(task.client)

        task.overdue_notified_at = timezone.now()
        task.save(update_fields=["overdue_notified_at"])
        new_due_at = timezone.localtime(timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(reverse("task_detail", args=[task.pk]), {"text": task.text, "due_at": new_due_at, "client": "", "deal": ""})
        self.assertRedirects(response, reverse("workday"))
        task.refresh_from_db()
        self.assertIsNone(task.overdue_notified_at)

        response = self.client.post(reverse("task_complete", args=[task.pk]))
        self.assertRedirects(response, reverse("workday"))
        task.refresh_from_db()
        self.assertTrue(task.is_done)
        self.assertIsNotNone(task.completed_at)

    def test_task_created_from_deal_inherits_its_client(self):
        due_at = timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

        response = self.client.post(reverse("task_create"), {
            "text": "Подготовить договор", "due_at": due_at, "client": "", "deal": self.deal.pk,
        })

        self.assertRedirects(response, reverse("workday"))
        task = ClientReminder.objects.get(text="Подготовить договор")
        self.assertEqual(task.client, self.client_record)
        self.assertEqual(task.deal, self.deal)

    def test_showing_has_separate_event_and_requires_result_when_completed(self):
        start = timezone.localtime(timezone.now() + timedelta(days=2))
        response = self.client.post(reverse("showing_create"), {
            "deal": self.deal.pk,
            "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "location": "У подъезда", "status": "planned", "outcome_note": "",
        })
        self.assertRedirects(response, reverse("workday"))
        showing = Showing.objects.get()
        self.assertEqual(showing.owner, self.owner)

        payload = {
            "deal": self.deal.pk,
            "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "location": "У подъезда", "status": "completed", "outcome_note": "",
        }
        response = self.client.post(reverse("showing_detail", args=[showing.pk]), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Запишите результат показа")
        response = self.client.post(reverse("showing_detail", args=[showing.pk]), {
            **payload, "outcome_note": "Клиенту понравилось",
            "next_action": "Уточнить решение", "next_action_due_at": (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        self.assertRedirects(response, reverse("workday"))
        showing.refresh_from_db()
        self.assertEqual(showing.status, "completed")
        self.assertEqual(showing.outcome_note, "Клиенту понравилось")
        self.assertEqual(showing.follow_up_task.text, "Уточнить решение")
        self.assertTrue(self.client_record.interactions.filter(text__contains="Клиенту понравилось").exists())
        Showing.objects.create(deal=self.deal, starts_at=start + timedelta(days=1))
        self.assertEqual(self.deal.showings.count(), 2)

    def test_foreign_tasks_and_showings_are_not_accessible(self):
        other_client = Client.objects.create(owner=self.other, name="Чужой", phone="+79990000023")
        other_property = Property.objects.create(owner=self.other, title="Чужой объект")
        other_deal = Deal.objects.create(owner=self.other, client=other_client, property=other_property)
        other_task = ClientReminder.objects.create(client=other_client, text="Чужая задача", due_at=timezone.now())
        other_showing = Showing.objects.create(deal=other_deal, starts_at=timezone.now() + timedelta(days=1))

        self.assertEqual(self.client.get(reverse("task_detail", args=[other_task.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("task_complete", args=[other_task.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("showing_detail", args=[other_showing.pk])).status_code, 404)
        response = self.client.post(reverse("showing_create"), {
            "deal": other_deal.pk, "starts_at": "2026-10-01T15:00", "status": "planned",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Showing.objects.filter(owner=self.owner).exists())

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="notifications@example.com",
        TELEGRAM_NOTIFICATIONS_ENABLED=False,
    )
    def test_overdue_digest_is_sent_once_and_only_after_success(self):
        RealtorProfile.objects.create(user=self.owner, email="owner@example.com")
        task = ClientReminder.objects.create(
            client=self.client_record, text="Позвонить по заявке", due_at=timezone.now() - timedelta(hours=2),
        )

        call_command("send_overdue_tasks", stdout=StringIO())
        task.refresh_from_db()
        self.assertIsNotNone(task.overdue_notified_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Позвонить по заявке", mail.outbox[0].body)

        call_command("send_overdue_tasks", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=False, TELEGRAM_NOTIFICATIONS_ENABLED=False)
    def test_overdue_task_stays_unsent_without_a_configured_channel(self):
        task = ClientReminder.objects.create(
            client=self.client_record, text="Задача без канала", due_at=timezone.now() - timedelta(hours=2),
        )

        call_command("send_overdue_tasks", stdout=StringIO())

        task.refresh_from_db()
        self.assertIsNone(task.overdue_notified_at)

    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=False, TELEGRAM_NOTIFICATIONS_ENABLED=True, TELEGRAM_BOT_TOKEN="test-token")
    @patch("properties.services.overdue_task_notification_service.requests.post")
    def test_overdue_digest_can_use_telegram(self, post):
        RealtorProfile.objects.create(user=self.owner, telegram_chat_id="123456")
        task = ClientReminder.objects.create(
            client=self.client_record, text="Позвонить клиенту", due_at=timezone.now() - timedelta(hours=2),
        )
        post.return_value.json.return_value = {"ok": True}

        call_command("send_overdue_tasks", stdout=StringIO())

        task.refresh_from_db()
        self.assertIsNotNone(task.overdue_notified_at)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"]["chat_id"], "123456")


class WorkdayMigrationTests(TransactionTestCase):
    def test_existing_reminders_and_deal_schedule_are_preserved(self):
        old_target = [("properties", "0034_deal_is_demo")]
        new_target = [("properties", "0035_workday")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        OldUser = old_apps.get_model("auth", "User")
        OldClient = old_apps.get_model("properties", "Client")
        OldProperty = old_apps.get_model("properties", "Property")
        OldDeal = old_apps.get_model("properties", "Deal")
        OldTask = old_apps.get_model("properties", "ClientReminder")
        user = OldUser.objects.create(username="migration-owner")
        client = OldClient.objects.create(owner_id=user.pk, name="Анна", phone="+79990000031")
        property = OldProperty.objects.create(owner_id=user.pk, title="Квартира")
        due_at = timezone.now() + timedelta(days=1)
        viewing_at = timezone.now() + timedelta(days=2)
        deal = OldDeal.objects.create(
            owner_id=user.pk, client_id=client.pk, property_id=property.pk,
            next_action="Подтвердить показ", next_action_at=due_at, viewing_at=viewing_at,
        )
        OldTask.objects.create(client_id=client.pk, text="Старое напоминание", due_at=due_at)

        executor = MigrationExecutor(connection)
        executor.migrate(new_target)

        self.assertEqual(ClientReminder.objects.filter(owner_id=user.pk).count(), 2)
        self.assertTrue(ClientReminder.objects.filter(deal_id=deal.pk, text="Подтвердить показ", due_at=due_at).exists())
        self.assertTrue(ClientReminder.objects.filter(client_id=client.pk, text="Старое напоминание").exists())
        self.assertTrue(Showing.objects.filter(deal_id=deal.pk, starts_at=viewing_at, status="planned").exists())


class LeadNotificationServiceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("agent", password="password")
        self.profile = RealtorProfile.objects.create(
            user=self.owner,
            email="agent@example.com",
            telegram_chat_id="123456",
        )
        self.property = Property.objects.create(title="Квартира у парка", owner=self.owner)
        self.lead = self.property.leads.create(
            name="Мария",
            phone="+351900000000",
            contact_purpose="viewing",
            message="Хочу посмотреть объект.",
        )

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="notifications@example.com",
    )
    def test_sends_new_lead_email_to_realtor_profile(self):
        with self.captureOnCommitCallbacks(execute=True):
            LeadNotificationService.notify_new_lead(self.lead, self.profile, "https://app.example.com/leads/1/")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["agent@example.com"])
        self.assertIn("Квартира у парка", mail.outbox[0].body)
        self.assertIn("https://app.example.com/leads/1/", mail.outbox[0].body)

    @override_settings(TELEGRAM_NOTIFICATIONS_ENABLED=True, TELEGRAM_BOT_TOKEN="test-token")
    @patch("properties.services.outbound_message_service.requests.post")
    def test_sends_new_lead_to_realtor_telegram_chat(self, post):
        response = Mock()
        response.json.return_value = {"ok": True}
        post.return_value = response

        with self.captureOnCommitCallbacks(execute=True):
            LeadNotificationService.notify_new_lead(self.lead, self.profile, "https://app.example.com/leads/1/")

        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.telegram.org/bottest-token/sendMessage")
        self.assertEqual(post.call_args.kwargs["data"]["chat_id"], "123456")
        self.assertIn("Мария", post.call_args.kwargs["data"]["text"])
