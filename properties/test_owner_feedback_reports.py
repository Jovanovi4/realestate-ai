from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Client, ClientInteraction, ClientReminder, Deal, Lead, Property, PropertyOwnerLink, Showing
from .services.owner_report_service import owner_report_data


class OwnerFeedbackAndReportTests(TestCase):
    def setUp(self):
        self.realtor = User.objects.create_user("owner-report-realtor", password="password")
        self.other = User.objects.create_user("owner-report-other", password="password")
        self.property = Property.objects.create(owner=self.realtor, title="Квартира для отчёта")
        self.owner_contact = Client.objects.create(
            owner=self.realtor, name="Собственник отчёта", phone="+79991112233", contact_role="owner",
        )
        self.buyer = Client.objects.create(
            owner=self.realtor, name="Секретный покупатель", phone="+79994445566",
        )
        self.link = PropertyOwnerLink.objects.create(
            property=self.property, client=self.owner_contact,
            agreement_expires_on=timezone.localdate() + timedelta(days=7),
        )
        self.deal = Deal.objects.create(
            owner=self.realtor, responsible=self.realtor, client=self.buyer, property=self.property,
        )
        self.start = timezone.localtime(timezone.now() - timedelta(hours=2)).replace(second=0, microsecond=0)
        self.showing = Showing.objects.create(owner=self.realtor, deal=self.deal, starts_at=self.start)
        self.client.force_login(self.realtor)

    def showing_payload(self, **changes):
        return {
            "deal": self.deal.pk,
            "starts_at": self.start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": "", "location": "", "status": "completed", "outcome_note": "Показ проведён",
            "next_action": "Уточнить решение покупателя",
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            **changes,
        }

    def plan_feedback(self):
        response = self.client.post(reverse("owner_feedback_plan", args=[self.showing.pk, self.link.pk]), {
            "due_at": timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        self.assertEqual(response.status_code, 302)
        return ClientReminder.objects.get(owner_showing=self.showing, client=self.owner_contact)

    def test_completed_showing_offers_separate_owner_task_and_records_feedback_once(self):
        detail_url = reverse("showing_detail", args=[self.showing.pk])
        response = self.client.post(detail_url, self.showing_payload())
        self.assertEqual(response.status_code, 302)
        self.assertIn("#owner-feedback", response["Location"])
        self.assertContains(self.client.get(detail_url), "Сообщить собственнику")
        self.showing.refresh_from_db()
        self.assertIsNotNone(self.showing.follow_up_task_id)
        self.assertEqual(self.showing.follow_up_task.client, self.buyer)
        self.assertFalse(ClientReminder.objects.filter(owner_showing=self.showing).exists())

        task = self.plan_feedback()
        self.assertEqual(task.owner, self.realtor)
        self.assertEqual(task.property, self.property)
        self.assertIsNone(task.deal_id)
        self.assertContains(self.client.get(reverse("work_items")), "Сообщить собственнику")
        self.plan_feedback()
        self.assertEqual(ClientReminder.objects.filter(owner_showing=self.showing).count(), 1)

        generic = self.client.post(reverse("task_complete", args=[task.pk]))
        self.assertIn("#owner-feedback", generic["Location"])
        task.refresh_from_db()
        self.assertFalse(task.is_done)
        complete_url = reverse("owner_feedback_complete", args=[self.showing.pk, task.pk])
        self.client.post(complete_url, {"method": "call", "feedback_note": ""})
        task.refresh_from_db()
        self.assertFalse(task.is_done)
        self.client.post(complete_url, {"method": "call", "feedback_note": "Сообщили, что показ состоялся"})
        task.refresh_from_db()
        self.assertTrue(task.is_done)
        self.assertEqual(task.outcome_note, "Сообщили, что показ состоялся")
        self.owner_contact.refresh_from_db()
        self.assertIsNotNone(self.owner_contact.first_contacted_at)
        self.assertEqual(ClientInteraction.objects.filter(client=self.owner_contact, property=self.property,
                                                          text__contains="Сообщён итог показа").count(), 1)
        self.client.post(complete_url, {"method": "call", "feedback_note": "Повтор"})
        self.assertEqual(ClientInteraction.objects.filter(client=self.owner_contact, property=self.property,
                                                          text__contains="Сообщён итог показа").count(), 1)

    def test_feedback_requires_completed_showing_and_is_cancelled_if_result_changes(self):
        plan_url = reverse("owner_feedback_plan", args=[self.showing.pk, self.link.pk])
        self.client.post(plan_url, {"due_at": timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")})
        self.assertFalse(ClientReminder.objects.filter(owner_showing=self.showing).exists())
        invalid = self.client.post(reverse("showing_detail", args=[self.showing.pk]), self.showing_payload(
            next_action="", next_action_due_at="",
        ))
        self.assertEqual(invalid.status_code, 200)
        self.assertNotContains(invalid, 'id="owner-feedback"')
        self.client.post(reverse("showing_detail", args=[self.showing.pk]), self.showing_payload())
        task = self.plan_feedback()
        self.client.post(reverse("showing_detail", args=[self.showing.pk]), self.showing_payload(
            status="cancelled", outcome_note="", next_action="", next_action_due_at="",
        ))
        task.refresh_from_db()
        self.assertIsNotNone(task.cancelled_at)
        self.assertNotContains(self.client.get(reverse("work_items")), reverse("task_detail", args=[task.pk]))
        self.client.post(reverse("owner_feedback_complete", args=[self.showing.pk, task.pk]), {
            "method": "call", "feedback_note": "Не должно сохраниться",
        })
        task.refresh_from_db()
        self.assertFalse(task.is_done)

    def test_each_owner_contact_has_separate_feedback_task(self):
        second = Client.objects.create(owner=self.realtor, name="Совладелец", phone="+79990001122", contact_role="owner")
        second_link = PropertyOwnerLink.objects.create(property=self.property, client=second, role="co_owner")
        self.client.post(reverse("showing_detail", args=[self.showing.pk]), self.showing_payload())
        first_task = self.plan_feedback()
        self.client.post(reverse("owner_feedback_plan", args=[self.showing.pk, second_link.pk]), {
            "due_at": timezone.localtime(timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        })
        second_task = ClientReminder.objects.get(owner_showing=self.showing, client=second)
        self.assertNotEqual(first_task.pk, second_task.pk)
        self.assertContains(self.client.get(reverse("showing_detail", args=[self.showing.pk])), "Совладелец")

    def test_task_cannot_link_a_deal_to_another_property(self):
        unrelated = Property.objects.create(owner=self.realtor, title="Другой объект")
        with self.assertRaises(ValidationError):
            ClientReminder.objects.create(
                owner=self.realtor, client=self.buyer, deal=self.deal,
                property=unrelated, text="Позвонить", due_at=timezone.now() + timedelta(hours=1),
            )

    def test_agreement_overview_and_private_report(self):
        self.client.post(reverse("showing_detail", args=[self.showing.pk]), self.showing_payload())
        task = self.plan_feedback()
        self.client.post(reverse("owner_feedback_complete", args=[self.showing.pk, task.pk]), {
            "method": "message", "feedback_note": "Итог передан",
        })
        Lead.objects.create(property=self.property, name=self.buyer.name, phone=self.buyer.phone)
        later = PropertyOwnerLink.objects.create(
            property=Property.objects.create(owner=self.realtor, title="Поздний объект"),
            client=self.owner_contact, agreement_expires_on=timezone.localdate() + timedelta(days=50),
        )
        listing = self.client.get(reverse("owner_agreement_list"))
        self.assertContains(listing, self.property.title)
        self.assertNotContains(listing, later.property.title)
        self.assertContains(self.client.get(reverse("owner_agreement_list") + "?scope=all"), later.property.title)

        report = self.client.get(reverse("owner_report", args=[self.link.pk]))
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report["Cache-Control"], "private, no-store")
        self.assertContains(report, "Собственник информирован")
        self.assertNotContains(report, self.buyer.name)
        self.assertNotContains(report, self.buyer.phone)
        data = owner_report_data(self.link)
        self.assertEqual(data["lead_count"], 1)
        self.assertEqual(data["completed_showing_count"], 1)
        self.assertEqual(data["reported_showing_count"], 1)
        self.assertNotIn(self.buyer.name, str(data["showings"]))
        pdf = self.client.get(reverse("owner_report_pdf", args=[self.link.pk]))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertEqual(pdf["Cache-Control"], "private, no-store")

        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("owner_agreement_list")), self.property.title)
        self.assertEqual(self.client.get(reverse("owner_report", args=[self.link.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("owner_report_pdf", args=[self.link.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("owner_feedback_plan", args=[self.showing.pk, self.link.pk]), {
            "due_at": timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        }).status_code, 404)
