import uuid
from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Agency, AgencyMembership, Client, ClientInteraction, ClientMergeRecord, ClientReminder,
    Deal, InboundEvent, Lead, OwnerInquiry, Property, PublicIntakeLink, RealtorProfile,
)
from .services.inbound_email_service import import_mailbox


@override_settings(TURNSTILE_ENABLED=False, BILLING_ENFORCEMENT_ENABLED=False)
class PublicIntakeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("intake-owner")
        self.agent = User.objects.create_user("intake-agent")
        self.outsider = User.objects.create_user("intake-outsider")
        self.agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=self.agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=self.agency, user=self.agent, role="agent")
        self.personal_link = PublicIntakeLink.objects.create(owner=self.owner)
        self.team_link = PublicIntakeLink.objects.create(agency=self.agency)

    def search_data(self, **changes):
        data = {
            "request_id": str(uuid.uuid4()), "source_detail": "Форма подбора · реклама",
            "name": "Ирина", "phone": "+79991112233", "interest_type": "buy",
            "message": "Ищу квартиру у метро", "personal_data_consent": "on",
        }
        data.update(changes)
        return data

    def test_search_form_creates_one_request_and_reuses_contact_on_retry(self):
        url = reverse("public_search_request", args=[self.personal_link.code])
        data = self.search_data()
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(self.client.post(url, data).status_code, 200)
        lead = Lead.objects.get()
        self.assertEqual((lead.source, lead.source_detail, lead.owner_id, lead.agency_id),
                         ("public_form", data["source_detail"], self.owner.pk, None))
        self.assertIsNotNone(lead.personal_data_consent_at)
        self.assertEqual(ClientReminder.objects.filter(lead=lead).count(), 1)
        self.assertEqual(InboundEvent.objects.count(), 1)
        self.assertAlmostEqual((lead.first_response_due_at - lead.created_at).total_seconds(), 1800, delta=2)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("lead_list")), "Ирина")
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[lead.pk])).status_code, 404)

    def test_owner_form_creates_team_inquiry_and_existing_contact_becomes_both(self):
        Client.objects.create(owner=self.owner, agency=self.agency, name="Ирина", phone="+79991112233")
        url = reverse("public_owner_request", args=[self.team_link.code])
        data = self.search_data(deal_type="sale", address="Улица Пушкина, 1", description="", expected_price="9000000")
        data.pop("interest_type")
        data.pop("message")
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(self.client.post(url, data).status_code, 200)
        inquiry = OwnerInquiry.objects.get()
        self.assertEqual((inquiry.agency_id, inquiry.source, inquiry.source_detail),
                         (self.agency.pk, "public_form", data["source_detail"]))
        self.assertEqual(inquiry.client.contact_role, "both")
        self.assertEqual(Client.objects.filter(agency=self.agency).count(), 1)
        self.client.force_login(self.owner)
        queue = self.client.get(reverse("lead_list"))
        self.assertEqual(queue.context["page_obj"].paginator.count, 1)
        self.assertContains(queue, "Продать · Улица Пушкина, 1")
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("owner_inquiry_detail", args=[inquiry.pk])).status_code, 200)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("owner_inquiry_detail", args=[inquiry.pk])).status_code, 404)

    def test_invalid_consent_and_disabled_link_create_nothing(self):
        url = reverse("public_search_request", args=[self.personal_link.code])
        response = self.client.post(url, self.search_data(personal_data_consent=""))
        self.assertContains(response, "необходимо согласие")
        self.assertFalse(Lead.objects.exists())
        self.personal_link.enabled = False
        self.personal_link.save(update_fields=["enabled"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_only_team_manager_can_pause_shared_form(self):
        self.client.force_login(self.agent)
        dashboard = reverse("public_intake_links")
        self.assertEqual(self.client.post(dashboard, {"scope": "team", "action": "pause"}).status_code, 404)
        self.team_link.refresh_from_db()
        self.assertTrue(self.team_link.enabled)
        self.client.force_login(self.owner)
        self.assertRedirects(
            self.client.post(dashboard, {"scope": "team", "action": "pause"}),
            reverse("lead_list"),
        )
        self.team_link.refresh_from_db()
        self.assertFalse(self.team_link.enabled)
        self.assertEqual(self.client.get(reverse("public_owner_request", args=[self.team_link.code])).status_code, 404)
        property = Property.objects.create(
            owner=self.owner, agency=self.agency, title="Командный объект", landing_published=True,
            landing_enabled_blocks={"alternative_requests": True},
        )
        self.assertContains(self.client.get(reverse("public_landing", args=[property.landing_slug])),
                            "Приём новых обращений временно приостановлен")

    def test_landing_list_has_no_separate_forms_or_inbox_shortcut(self):
        self.client.force_login(self.owner)
        landings = self.client.get(reverse("landing_list"))
        self.assertEqual(landings.status_code, 200)
        self.assertEqual((landings.context["published_count"], landings.context["draft_count"]), (0, 0))
        self.assertNotContains(landings, "Формы обращений")
        self.assertNotContains(landings, "Входящая почта")
        self.assertNotContains(self.client.get(reverse("lead_list")), "Формы обращений")
        self.assertRedirects(
            self.client.get(reverse("public_intake_links")),
            reverse("lead_list"),
        )
        self.assertEqual(self.client.get(reverse("public_search_request", args=[self.personal_link.code])).status_code, 200)
        self.assertEqual(self.client.get(reverse("public_owner_request", args=[self.team_link.code])).status_code, 200)

    def test_unprocessed_mail_stays_accessible_from_leads_only_in_its_workspace(self):
        InboundEvent.objects.create(link=self.personal_link, channel="email", external_id="mail-1")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("lead_list")), "Письма для разбора: 1")
        self.client.force_login(self.agent)
        self.assertNotContains(self.client.get(reverse("lead_list")), "Письма для разбора")

    def test_landing_retry_with_same_request_id_keeps_one_lead(self):
        property = Property.objects.create(owner=self.owner, title="Квартира в центре", landing_published=True)
        url = reverse("public_landing", args=[property.landing_slug])
        request_id = str(uuid.uuid4())
        data = {
            "request_id": request_id, "contact_purpose": "viewing", "name": "Мария",
            "phone": "+79991112236", "message": "Хочу посмотреть", "personal_data_consent": "on",
        }
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(Lead.objects.filter(property=property).count(), 1)
        self.assertEqual(InboundEvent.objects.filter(channel="landing", lead__property=property).count(), 1)
        self.assertEqual(Lead.objects.get(property=property).source_detail, "Лендинг · Квартира в центре")

    def test_optional_block_renders_in_all_templates_and_keeps_original_lead_form(self):
        for template in ("classic", "modern", "premium"):
            property = Property.objects.create(
                owner=self.owner, title=f"Объект {template}", landing_template=template,
                landing_published=True, landing_enabled_blocks={"alternative_requests": True},
            )
            response = self.client.get(reverse("public_landing", args=[property.landing_slug]))
            self.assertContains(response, "Подобрать другую недвижимость")
            self.assertContains(response, "Продать или сдать свой объект")
            self.assertContains(response, 'name="intake_kind" value="search"')
            self.assertContains(response, 'name="intake_kind" value="owner"')
            self.assertContains(response, 'id="leadModal"')

    def test_embedded_search_creates_one_propertyless_lead_with_landing_source(self):
        property = Property.objects.create(
            owner=self.owner, title="Квартира у парка", landing_published=True,
            landing_enabled_blocks={"alternative_requests": True},
        )
        url = reverse("public_landing", args=[property.landing_slug])
        data = {
            "intake_kind": "search", "search-request_id": str(uuid.uuid4()),
            "search-source_detail": "Подделанный источник", "search-name": "Ирина",
            "search-phone": "+79991112233", "search-interest_type": "buy",
            "search-message": "Ищу другой район", "search-personal_data_consent": "on",
        }
        self.assertContains(self.client.post(url, data), "Ваше обращение отправлено")
        self.assertContains(self.client.post(url, data), "Ваше обращение отправлено")
        lead = Lead.objects.get()
        self.assertIsNone(lead.property_id)
        self.assertEqual((lead.owner_id, lead.agency_id, lead.source), (self.owner.pk, None, "landing"))
        self.assertEqual(lead.source_detail, f"Лендинг · {property.title} · #{property.pk}")
        self.assertEqual(InboundEvent.objects.filter(channel="landing", lead=lead).count(), 1)

    @patch("properties.public_intake_views.LeadNotificationService.notify_new_lead")
    def test_team_search_notifies_realtor_of_the_landing(self, notify):
        RealtorProfile.objects.get_or_create(user=self.agent)
        property = Property.objects.create(
            owner=self.agent, agency=self.agency, title="Командный объект", landing_published=True,
            landing_enabled_blocks={"alternative_requests": True},
        )
        data = {
            "intake_kind": "search", "search-request_id": str(uuid.uuid4()),
            "search-name": "Ирина", "search-phone": "+79991112233",
            "search-interest_type": "buy", "search-message": "Нужен другой объект",
            "search-personal_data_consent": "on",
        }
        self.client.post(reverse("public_landing", args=[property.landing_slug]), data)
        self.assertEqual(Lead.objects.get().assigned_to_id, self.agent.pk)
        self.assertEqual(notify.call_args.args[1].user_id, self.agent.pk)

    def test_embedded_owner_form_uses_team_workspace_and_requires_consent(self):
        property = Property.objects.create(
            owner=self.agent, agency=self.agency, title="Командный объект", landing_published=True,
            landing_enabled_blocks={"alternative_requests": True},
        )
        url = reverse("public_landing", args=[property.landing_slug])
        data = {
            "intake_kind": "owner", "owner-request_id": str(uuid.uuid4()),
            "owner-name": "Михаил", "owner-phone": "+79991112234",
            "owner-deal_type": "sale", "owner-address": "Новая улица, 1",
        }
        invalid = self.client.post(url, data)
        self.assertContains(invalid, "необходимо согласие")
        self.assertContains(invalid, 'name="owner-deal_type"')
        self.assertFalse(OwnerInquiry.objects.exists())
        data["owner-personal_data_consent"] = "on"
        self.assertContains(self.client.post(url, data), "Ваше обращение отправлено")
        inquiry = OwnerInquiry.objects.get()
        self.assertEqual((inquiry.agency_id, inquiry.owner_id, inquiry.source),
                         (self.agency.pk, self.owner.pk, "landing"))
        self.assertEqual(inquiry.assigned_to_id, self.agent.pk)
        self.assertEqual(inquiry.source_detail, f"Лендинг · {property.title} · #{property.pk}")
        self.assertEqual(InboundEvent.objects.filter(channel="landing", owner_inquiry=inquiry).count(), 1)

    def test_hidden_block_rejects_embedded_submission(self):
        property = Property.objects.create(owner=self.owner, title="Скрытый блок", landing_published=True)
        url = reverse("public_landing", args=[property.landing_slug])
        self.assertNotContains(self.client.get(url), "Подобрать другую недвижимость")
        self.assertEqual(self.client.post(url, {"intake_kind": "search"}).status_code, 404)

    def test_existing_landing_order_is_preserved_in_editor(self):
        property = Property.objects.create(
            owner=self.owner, title="Старый лендинг",
            landing_block_order=["hero", "contact", "facts", "description", "gallery", "mortgage", "trust"],
            landing_enabled_blocks={"hero": True, "contact": True},
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("edit_property", args=[property.pk]))
        blocks = response.context["form"].landing_blocks
        self.assertEqual([key for key, _, _ in blocks][:3], ["hero", "contact", "facts"])
        self.assertEqual(blocks[-1], ("alternative_requests", "Подбор и обращение собственника", False))


class ClientMergeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("merge-owner")
        self.outsider = User.objects.create_user("merge-outsider")
        self.target = Client.objects.create(owner=self.user, name="Мария", phone="+79991112233")
        self.source = Client.objects.create(owner=self.user, name="Мария", phone="89991112233", contact_role="owner")
        self.property = Property.objects.create(owner=self.user, title="Квартира")
        self.client.force_login(self.user)

    def test_merge_moves_leads_deals_tasks_and_history(self):
        lead = Lead.objects.create(owner=self.user, client=self.source, name="Мария", phone=self.source.phone,
                                   next_action="Позвонить")
        deal = Deal.objects.create(owner=self.user, client=self.source, property=self.property)
        reminder = ClientReminder.objects.create(owner=self.user, client=self.source, text="Отправить документы")
        interaction = ClientInteraction.objects.create(client=self.source, interaction_type="call", text="Обсудили условия")
        url = reverse("client_merge", args=[self.target.pk, self.source.pk])
        self.assertContains(self.client.get(reverse("client_detail", args=[self.target.pk])), "Возможные совпадения")
        self.assertContains(self.client.get(url), "Объединить контакты")
        self.assertRedirects(self.client.post(url, {"confirm": "merge"}), reverse("client_detail", args=[self.target.pk]))
        self.assertFalse(Client.objects.filter(pk=self.source.pk).exists())
        for record in (lead, deal, reminder, interaction):
            record.refresh_from_db()
            self.assertEqual(record.client_id, self.target.pk)
        self.target.refresh_from_db()
        self.assertEqual(self.target.contact_role, "both")
        self.assertEqual(ClientMergeRecord.objects.get().source_id, self.source.pk)

    def test_conflicting_active_deal_blocks_merge_without_changes(self):
        Deal.objects.create(owner=self.user, client=self.target, property=self.property)
        Deal.objects.create(owner=self.user, client=self.source, property=self.property)
        url = reverse("client_merge", args=[self.target.pk, self.source.pk])
        self.assertContains(self.client.get(url), "активная сделка")
        self.assertRedirects(self.client.post(url, {"confirm": "merge"}), url)
        self.assertEqual(Client.objects.count(), 2)
        self.assertFalse(ClientMergeRecord.objects.exists())

    def test_cannot_merge_another_users_contacts(self):
        self.client.force_login(self.outsider)
        url = reverse("client_merge", args=[self.target.pk, self.source.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"confirm": "merge"}).status_code, 404)

    def test_recording_call_can_schedule_next_step(self):
        due = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(reverse("client_interaction_create", args=[self.target.pk]), {
            "interaction_type": "call", "text": "Обсудили бюджет",
            "next_action": "Отправить подборку", "next_action_due_at": due,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ClientInteraction.objects.filter(client=self.target, text="Обсудили бюджет").exists())
        self.assertTrue(ClientReminder.objects.filter(client=self.target, text="Отправить подборку").exists())

    def test_next_step_requires_due_date_and_rolls_back_call(self):
        response = self.client.post(reverse("client_interaction_create", args=[self.target.pk]), {
            "interaction_type": "call", "text": "Обсудили бюджет", "next_action": "Отправить подборку",
        })
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Укажите срок следующего шага", status_code=400)
        self.assertFalse(ClientInteraction.objects.exists())
        self.assertFalse(ClientReminder.objects.exists())


@override_settings(TURNSTILE_ENABLED=False, BILLING_ENFORCEMENT_ENABLED=False)
class InboundEmailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("mail-owner")
        self.outsider = User.objects.create_user("mail-outsider")
        self.link = PublicIntakeLink.objects.create(owner=self.user)
        self.client.force_login(self.user)

    def test_imap_resume_and_review_to_lead_preserve_received_time(self):
        message = EmailMessage()
        message["From"] = "Ирина <irina@example.test>"
        message["Subject"] = "Подбор квартиры"
        message["Date"] = "Mon, 12 Oct 2026 10:00:00 +0000"
        message.set_content("Телефон: +79991112233\nИщу квартиру до 8 млн")
        raw = message.as_bytes()

        class FakeIMAP:
            def __init__(self, host, port, timeout):
                self.host = host

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def login(self, username, password):
                return "OK", []

            def select(self, folder, readonly):
                return "OK", []

            def response(self, name):
                return "UIDVALIDITY", [b"41"]

            def uid(self, action, *args):
                if action == "SEARCH":
                    return "OK", [b"1"]
                return "OK", [(b'1 (INTERNALDATE "13-Oct-2026 11:30:00 +0000" BODY[])', raw)]

        options = dict(link=self.link, host="imap.example.test", username="test@example.test",
                       password="private", folder="REAICRM", imap_factory=FakeIMAP)
        self.assertEqual(import_mailbox(**options), 1)
        self.assertEqual(import_mailbox(**options), 0)
        event = InboundEvent.objects.get(channel="email")
        self.assertEqual(self.client.get(reverse("inbound_email_detail", args=[event.pk])).status_code, 200)
        response = self.client.post(reverse("inbound_email_detail", args=[event.pk]), {
            "kind": "search", "name": "Ирина", "phone": "+79991112233",
            "interest_type": "buy", "message": event.body,
        })
        lead = Lead.objects.get(source="email")
        self.assertRedirects(response, reverse("lead_detail", args=[lead.pk]))
        event.refresh_from_db()
        self.assertEqual(event.lead_id, lead.pk)
        self.assertEqual(lead.created_at, event.received_at)
        self.assertEqual(event.received_at.isoformat(), "2026-10-13T11:30:00+00:00")
        self.assertEqual(lead.source_detail, "Ирина <irina@example.test> · Подбор квартиры")
        self.assertEqual(self.client.get(reverse("inbound_email_list")).status_code, 200)
        self.assertNotContains(self.client.get(reverse("inbound_email_list")), "Подбор квартиры")
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("inbound_email_detail", args=[event.pk])).status_code, 404)

    def test_email_owner_conversion_and_ignore_are_idempotent(self):
        event = InboundEvent.objects.create(
            link=self.link, channel="email", external_id="mail:1", sender="owner@example.test",
            subject="Сдать квартиру", body="Квартира в центре", received_at=timezone.now(),
        )
        response = self.client.post(reverse("inbound_email_detail", args=[event.pk]), {
            "kind": "owner", "name": "Анна", "phone": "+79991112235", "deal_type": "rent",
            "message": "Квартира в центре", "address": "Ленина, 3",
        })
        inquiry = OwnerInquiry.objects.get(source="email")
        self.assertRedirects(response, reverse("owner_inquiry_detail", args=[inquiry.pk]))
        self.assertEqual(InboundEvent.objects.get(pk=event.pk).owner_inquiry_id, inquiry.pk)
        self.assertEqual(OwnerInquiry.objects.count(), 1)
        queue = self.client.get(reverse("lead_list"))
        self.assertEqual(queue.context["page_obj"].paginator.count, 1)
        self.assertContains(queue, "Сдать · Ленина, 3")
        ignored = InboundEvent.objects.create(
            link=self.link, channel="email", external_id="mail:2", sender="spam@example.test",
            subject="Реклама", body="Текст", received_at=timezone.now(),
        )
        self.assertRedirects(self.client.post(reverse("inbound_email_detail", args=[ignored.pk]), {"action": "ignore"}),
                             reverse("inbound_email_list"))
        ignored.refresh_from_db()
        self.assertIsNotNone(ignored.ignored_at)
