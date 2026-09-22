from io import BytesIO
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from PIL import Image

from .models import AIContent, Client, ClientReminder, Lead, Property, PropertyImage, RealtorProfile, UserLegalAcceptance
from .forms import AIContentEditForm, AIRequestForm
from .ai_views import get_ai_history_context
from .services.ai_service import AIServiceError
from .services.lead_notification_service import LeadNotificationService


class AccountAndPropertyAccessTests(TestCase):
    def setUp(self):
        cache.clear()

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

        self.assertContains(response, "Начните с цены, адреса и главной фотографии")

    def test_empty_text_and_landing_sections_show_contextual_hints(self):
        owner = User.objects.create_user("agent", password="password")
        property = Property.objects.create(title="Черновик", owner=owner)
        self.client.force_login(owner)

        response = self.client.get(reverse("edit_property", args=[property.pk]))

        self.assertContains(response, "Можно написать текст вручную или получить черновик от ИИ")
        self.assertContains(response, "Предпросмотр покажет лендинг так, как его увидит клиент")

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

        self.assertContains(response, "css/rieltor-ui.css")
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
        self.assertContains(response, "Этап: В работе")
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
        Client.objects.create(owner=owner, name="Борис", phone="+79990000001", status="won")
        Client.objects.create(owner=other, name="Чужой клиент", phone="+79990000002", status="new")
        self.client.force_login(owner)

        response = self.client.get(reverse("client_board"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([(status, len(clients)) for status, _, clients in response.context["columns"]],
                         [("new", 1), ("in_progress", 0), ("viewing", 0), ("negotiation", 0), ("won", 1), ("lost", 0)])
        self.assertContains(response, 'class="client-board__column is-new"')
        self.assertNotContains(response, "Чужой клиент")

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

    def test_starting_a_property_opens_a_saved_draft_with_ai_tools(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)

        response = self.client.get(reverse("create_property"))

        draft = Property.objects.get(owner=user)
        self.assertEqual(draft.title, "Новый объект")
        self.assertEqual(draft.status, "draft")
        self.assertRedirects(response, reverse("edit_property", args=[draft.pk]))

        response = self.client.get(reverse("edit_property", args=[draft.pk]))
        self.assertContains(response, "data-ai-bundle-trigger")
        self.assertContains(response, 'id="ai-inline-config"')

    def test_new_user_can_create_personal_demo_data(self):
        user = User.objects.create_user("new-agent", password="password")
        self.client.force_login(user)

        response = self.client.post(reverse("onboarding_demo_create"))

        self.assertRedirects(response, reverse("property_list"))
        demo_property = Property.objects.get(owner=user, is_demo=True)
        self.assertTrue(demo_property.landing_published)
        self.assertEqual(demo_property.images.count(), 3)
        self.assertIn("demo-demo-living-room", demo_property.images.get(is_primary=True).image.name)
        self.assertEqual(demo_property.leads.filter(is_demo=True).count(), 2)
        self.assertEqual(Client.objects.filter(owner=user, is_demo=True).count(), 2)
        self.assertTrue(RealtorProfile.objects.get(user=user).demo_data_created)

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

    def test_property_detail_renders_preview_presentation_and_danger_action(self):
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
        self.assertContains(response, 'class="detail-danger"')

    def test_lead_detail_keeps_client_and_property_navigation(self):
        owner = User.objects.create_user("detail-owner", password="password")
        property = Property.objects.create(title="Квартира", owner=owner)
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        lead = Lead.objects.create(property=property, client=client, name="Анна", phone=client.phone, message="Хочу посмотреть")
        self.client.force_login(owner)

        response = self.client.get(reverse("lead_detail", args=[lead.pk]))

        self.assertContains(response, 'class="detail-two-column"')
        self.assertContains(response, "Хочу посмотреть")
        self.assertContains(response, reverse("client_detail", args=[client.pk]))
        self.assertContains(response, reverse("property_detail", args=[property.pk]))
        self.assertContains(response, 'data-confirm-title="Удалить заявку?"')

    def test_client_detail_shows_next_reminder_and_history_entry_stays_on_tab(self):
        owner = User.objects.create_user("detail-owner", password="password")
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        ClientReminder.objects.create(client=client, text="Позвонить завтра", due_at=timezone.now() + timedelta(days=1))
        self.client.force_login(owner)

        response = self.client.get(reverse("client_detail", args=[client.pk]))

        self.assertContains(response, 'class="detail-summary-bar"')
        self.assertContains(response, "Позвонить завтра")
        self.assertContains(response, f'id="client-status-{client.pk}"')
        self.assertContains(response, 'id="client-history"')

        interaction_response = self.client.post(reverse("client_interaction_create", args=[client.pk]), {
            "interaction_type": "note", "text": "Обсудили условия",
        })
        self.assertRedirects(interaction_response, f"{reverse('client_detail', args=[client.pk])}#client-history", fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("client_detail", args=[client.pk])), "Обсудили условия")

    def test_client_status_quick_action_keeps_htmx_control(self):
        owner = User.objects.create_user("detail-owner", password="password")
        client = Client.objects.create(owner=owner, name="Анна", phone="+79990000000")
        self.client.force_login(owner)

        response = self.client.post(reverse("client_status_update", args=[client.pk]),
                                    {"status": "in_progress"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="client-status-{client.pk}"')
        self.assertContains(response, 'class="detail-status-control"')
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
        LeadNotificationService.notify_new_lead(self.lead, self.profile, "https://app.example.com/leads/1/")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["agent@example.com"])
        self.assertIn("Квартира у парка", mail.outbox[0].body)
        self.assertIn("https://app.example.com/leads/1/", mail.outbox[0].body)

    @override_settings(TELEGRAM_NOTIFICATIONS_ENABLED=True, TELEGRAM_BOT_TOKEN="test-token")
    @patch("properties.services.lead_notification_service.requests.post")
    def test_sends_new_lead_to_realtor_telegram_chat(self, post):
        response = Mock()
        response.json.return_value = {"ok": True}
        post.return_value = response

        LeadNotificationService.notify_new_lead(self.lead, self.profile, "https://app.example.com/leads/1/")

        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.telegram.org/bottest-token/sendMessage")
        self.assertEqual(post.call_args.kwargs["data"]["chat_id"], "123456")
        self.assertIn("Мария", post.call_args.kwargs["data"]["text"])
