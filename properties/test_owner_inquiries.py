from datetime import timedelta
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientInteraction, ClientReminder, OwnerInquiry, Property, PropertyOwnerLink, RealtorProfile
from .services.staff_display import staff_display_name


class OwnerInquiryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner-inquiries", password="password")
        self.other = User.objects.create_user("other-inquiries", password="password")
        self.client.force_login(self.user)

    def create_data(self, **changes):
        data = {
            "contact_name": "Мария", "contact_phone": "+7 (999) 100-10-10",
            "deal_type": "sale", "address": "Москва, Лесная 5", "description": "Двухкомнатная квартира",
            "expected_price": "7800000", "currency": "RUB", "source": "recommendation",
            "assigned_to": self.user.pk,
            "first_response_due_at": timezone.localtime(timezone.now() + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
        }
        data.update(changes)
        return data

    def process_data(self, **changes):
        data = {"result": "agreed", "result_note": "Согласовали условия работы",
                "next_action": "Подготовить объект",
                "next_action_due_at": timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")}
        data.update(changes)
        return data

    def test_create_without_property_and_show_in_tasks(self):
        response = self.client.post(reverse("owner_inquiry_create"), self.create_data())
        self.assertEqual(response.status_code, 302, response.context["form"].errors if response.context else "")
        inquiry = OwnerInquiry.objects.get()
        self.assertIsNone(inquiry.property_id)
        self.assertEqual(inquiry.client.contact_role, "owner")
        self.assertEqual(inquiry.assigned_to, self.user)
        self.assertEqual(inquiry.source, "recommendation")
        self.assertEqual(Property.objects.count(), 0)
        self.assertContains(self.client.get(reverse("owner_inquiry_list")), "Мария")
        self.assertContains(self.client.get(reverse("workday")), "Ответить собственнику")
        self.assertContains(self.client.get(reverse("work_items") + "?kind=owner"), "Ответить собственнику")

    def test_owner_inquiry_uses_readable_price_and_assignee_name(self):
        self.user.username = "+79995554433"
        self.user.save(update_fields=["username"])
        RealtorProfile.objects.update_or_create(user=self.user, defaults={"display_name": "Анна Риелтор"})
        self.client.post(reverse("owner_inquiry_create"), self.create_data())
        inquiry = OwnerInquiry.objects.get()

        self.assertContains(self.client.get(reverse("owner_inquiry_list")), "Анна Риелтор")
        detail = self.client.get(reverse("owner_inquiry_detail", args=[inquiry.pk]))
        self.assertContains(detail, "7 800 000 ₽")
        self.assertContains(detail, "Ответственный: Анна Риелтор")
        client_detail = self.client.get(reverse("client_detail", args=[inquiry.client_id]))
        self.assertContains(client_detail, "Ответственный: Анна Риелтор")

        RealtorProfile.objects.filter(user=self.user).update(display_name="")
        self.user.refresh_from_db()
        self.assertEqual(staff_display_name(self.user), f"Риелтор №{self.user.pk}")

    def test_existing_contact_is_reused_and_not_duplicated(self):
        contact = Client.objects.create(owner=self.user, name="Мария", phone="+79991001010")
        data = self.create_data(client=contact.pk, contact_name="", contact_phone="")
        response = self.client.post(reverse("owner_inquiry_create"), data)
        self.assertEqual(response.status_code, 302, response.context["form"].errors if response.context else "")
        self.assertEqual(Client.objects.count(), 1)
        contact.refresh_from_db()
        self.assertEqual(contact.contact_role, "both")
        self.assertEqual(OwnerInquiry.objects.get().client, contact)

    def test_owner_inquiry_does_not_hide_unrelated_client_task(self):
        self.client.post(reverse("owner_inquiry_create"), self.create_data())
        inquiry = OwnerInquiry.objects.get()
        reminder = ClientReminder.objects.create(
            owner=self.user, client=inquiry.client, text="Отправить договор",
            due_at=timezone.now() + timedelta(days=1),
        )

        response = self.client.get(reverse("client_detail", args=[inquiry.client_id]))

        self.assertEqual(response.context["priority_owner_inquiry"].pk, inquiry.pk)
        self.assertEqual([task.pk for task in response.context["reminders"]], [reminder.pk])
        self.assertContains(response, "Отправить договор")

    def test_owner_overview_limits_inquiries_and_full_list_stays_filtered(self):
        self.client.post(reverse("owner_inquiry_create"), self.create_data())
        contact = OwnerInquiry.objects.get().client
        for index in range(3):
            self.client.post(reverse("owner_inquiry_create"), self.create_data(
                client=contact.pk, contact_name="", contact_phone="", address=f"Адрес {index}",
            ))
        other = Client.objects.create(owner=self.user, name="Другой собственник", phone="+79991001011", contact_role="owner")
        self.client.post(reverse("owner_inquiry_create"), self.create_data(
            client=other.pk, contact_name="", contact_phone="", address="Чужой адрес",
        ))

        detail = self.client.get(reverse("client_detail", args=[contact.pk]))
        self.assertEqual(detail.context["owner_inquiries_total"], 4)
        self.assertEqual(len(detail.context["owner_inquiries"]), 3)
        self.assertContains(detail, f"{reverse('owner_inquiry_list')}?client={contact.pk}&amp;status=all")

        full_list = self.client.get(reverse("owner_inquiry_list"), {"client": contact.pk, "status": "all"})
        self.assertEqual(full_list.context["page_obj"].paginator.count, 4)
        self.assertNotContains(full_list, "Чужой адрес")
        self.assertContains(full_list, f"status=completed&amp;client={contact.pk}")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("owner_inquiry_list"), {"client": contact.pk}).status_code, 404)

    def test_call_result_and_conversion_link_without_duplicate(self):
        self.client.post(reverse("owner_inquiry_create"), self.create_data())
        inquiry = OwnerInquiry.objects.get()
        self.assertNotContains(self.client.get(reverse("owner_inquiry_detail", args=[inquiry.pk])), "Создать объект из обращения")
        response = self.client.post(reverse("owner_inquiry_process", args=[inquiry.pk]), self.process_data())
        self.assertEqual(response.status_code, 302)
        inquiry.refresh_from_db()
        self.assertIsNotNone(inquiry.first_contacted_at)
        self.assertEqual(inquiry.result, "agreed")
        self.assertEqual(inquiry.next_action, "Подготовить объект")
        self.assertTrue(ClientInteraction.objects.filter(client=inquiry.client, text__contains="Согласовали").exists())

        url = reverse("create_property") + f"?owner_inquiry={inquiry.pk}"
        create_page = self.client.get(url)
        self.assertEqual(create_page.status_code, 200)
        self.assertEqual(create_page.context["form"].initial["address"], inquiry.address)
        self.assertEqual(create_page.context["form"].initial["price"], inquiry.expected_price)
        property_data = {
            "owner_inquiry": inquiry.pk, "request_key": create_page.context["resource_request_key"],
            "title": inquiry.address, "property_type": "apartment", "deal_type": "sale", "status": "draft",
            "price": "7800000", "currency": "RUB", "address": inquiry.address,
            "landing_template": "classic", "avito_operation": "sell",
        }
        created = self.client.post(reverse("create_property"), property_data)
        self.assertEqual(created.status_code, 302, created.content.decode()[:1200])
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.property.status, "draft")
        link = PropertyOwnerLink.objects.get(property=inquiry.property)
        self.assertEqual(link.client, inquiry.client)
        self.assertIsNone(link.agreed_price)
        self.assertEqual(Client.objects.count(), 1)
        self.assertRedirects(self.client.get(url), reverse("owner_inquiry_detail", args=[inquiry.pk]), fetch_redirect_response=False)
        retry = self.client.post(reverse("create_property"), {**property_data, "request_key": str(uuid4())})
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(Property.objects.count(), 1)

    def test_incomplete_result_cannot_convert(self):
        self.client.post(reverse("owner_inquiry_create"), self.create_data())
        inquiry = OwnerInquiry.objects.get()
        response = self.client.post(reverse("owner_inquiry_process", args=[inquiry.pk]), self.process_data(next_action=""))
        self.assertEqual(response.status_code, 400)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.result, "pending")
        response = self.client.post(reverse("create_property"), {
            "owner_inquiry": inquiry.pk, "title": "Не согласован", "property_type": "apartment", "deal_type": "sale",
            "status": "draft", "currency": "RUB", "landing_template": "classic", "avito_operation": "sell",
        })
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Property.objects.exists())
        self.assertRedirects(self.client.get(reverse("create_property") + f"?owner_inquiry={inquiry.pk}"),
                             reverse("owner_inquiry_detail", args=[inquiry.pk]), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("create_property") + "?owner_inquiry=invalid").status_code, 404)

    def test_declined_inquiry_can_be_saved_without_next_action(self):
        self.client.post(reverse("owner_inquiry_create"), self.create_data())
        inquiry = OwnerInquiry.objects.get()

        response = self.client.post(reverse("owner_inquiry_process", args=[inquiry.pk]), self.process_data(
            result="declined", result_note="Собственник передумал", next_action="", next_action_due_at="",
        ))

        self.assertRedirects(response, reverse("owner_inquiry_detail", args=[inquiry.pk]))
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.result, "declined")
        self.assertEqual(inquiry.next_action, "")
        self.assertIsNotNone(inquiry.first_contacted_at)
        self.assertFalse(inquiry.is_overdue)

    def test_foreign_contact_and_inquiry_are_invisible(self):
        foreign = Client.objects.create(owner=self.other, name="Чужой", phone="+79992002020")
        response = self.client.post(reverse("owner_inquiry_create"), self.create_data(
            client=foreign.pk, contact_name="", contact_phone="",
        ))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(OwnerInquiry.objects.exists())
        self.client.post(reverse("owner_inquiry_create"), self.create_data())
        inquiry = OwnerInquiry.objects.get()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("owner_inquiry_detail", args=[inquiry.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("owner_inquiry_process", args=[inquiry.pk]), self.process_data()).status_code, 404)
        self.assertEqual(self.client.get(reverse("create_property") + f"?owner_inquiry={inquiry.pk}").status_code, 404)

    def test_team_assignee_can_process_shared_inquiry(self):
        agency = Agency.objects.create(owner=self.user, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="agent")
        response = self.client.post(reverse("owner_inquiry_create"), self.create_data(assigned_to=self.other.pk))
        self.assertEqual(response.status_code, 302, response.context["form"].errors if response.context else "")
        inquiry = OwnerInquiry.objects.get()
        self.assertEqual(inquiry.agency, agency)
        self.client.force_login(self.other)
        self.assertContains(self.client.get(reverse("workday")), "Ответить собственнику")
        self.assertEqual(self.client.post(reverse("owner_inquiry_assign", args=[inquiry.pk]), {
            "assigned_to": self.user.pk,
        }).status_code, 404)
        self.assertEqual(self.client.post(reverse("owner_inquiry_process", args=[inquiry.pk]), self.process_data()).status_code, 302)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.result, "agreed")
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("owner_inquiry_detail", args=[inquiry.pk])), "Сменить")
        self.assertEqual(self.client.post(reverse("owner_inquiry_assign", args=[inquiry.pk]), {
            "assigned_to": self.user.pk,
        }).status_code, 302)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.assigned_to, self.user)
        self.client.post(reverse("owner_inquiry_assign", args=[inquiry.pk]), {"assigned_to": self.other.pk})
        membership = AgencyMembership.objects.get(agency=agency, user=self.other)
        self.assertEqual(self.client.post(reverse("agency_member_remove", args=[membership.pk])).status_code, 302)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.assigned_to, self.user)
