from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Agency, AgencyMembership, Client, ClientReminder, Deal, OwnerInquiry,
    Property, Showing, same_workspace,
)


class AgencyCreationScopeTests(TestCase):
    def test_creation_moves_owner_inquiries_and_showing_feedback_with_their_references(self):
        founder = User.objects.create_user("team-founder")
        agent = User.objects.create_user("team-agent")
        outsider = User.objects.create_user("team-outsider")
        home = Property.objects.create(owner=founder, title="Дом собственника")
        owner_contact = Client.objects.create(
            owner=founder, name="Собственник", phone="+79990001111", contact_role="owner",
        )
        buyer = Client.objects.create(owner=founder, name="Покупатель", phone="+79990002222")
        open_inquiry = OwnerInquiry.objects.create(
            owner=founder, assigned_to=founder, client=owner_contact,
            deal_type="sale", address="Дом без объекта",
        )
        agreed_inquiry = OwnerInquiry.objects.create(
            owner=founder, assigned_to=founder, client=owner_contact, deal_type="sale",
            address="Дом собственника", result="agreed",
            first_contacted_at=timezone.now(), property=home,
        )
        deal = Deal.objects.create(owner=founder, responsible=founder, client=buyer, property=home)
        showing = Showing.objects.create(owner=founder, deal=deal, starts_at=timezone.now())
        feedback_task = ClientReminder.objects.create(
            owner=founder, client=owner_contact, property=home,
            owner_showing=showing, text="Сообщить собственнику итог показа",
        )
        other_contact = Client.objects.create(owner=outsider, name="Другой собственник", phone="+79990003333")
        other_inquiry = OwnerInquiry.objects.create(
            owner=outsider, assigned_to=outsider, client=other_contact,
            deal_type="sale", address="Другой дом",
        )

        self.client.force_login(founder)
        response = self.client.post(reverse("agency_create"), {"name": "Команда"})
        self.assertRedirects(response, reverse("agency_dashboard"))
        agency = Agency.objects.get(owner=founder)

        for record in (home, owner_contact, buyer, open_inquiry, agreed_inquiry,
                       deal, showing, feedback_task):
            record.refresh_from_db()
            self.assertEqual(record.agency, agency)
        self.assertTrue(same_workspace(open_inquiry, owner_contact))
        self.assertTrue(same_workspace(agreed_inquiry, home))
        self.assertTrue(same_workspace(feedback_task, showing))
        self.assertEqual(open_inquiry.assigned_to, founder)
        other_inquiry.refresh_from_db()
        self.assertIsNone(other_inquiry.agency_id)

        AgencyMembership.objects.create(agency=agency, user=agent, role="agent")
        self.client.force_login(agent)
        self.assertEqual(self.client.get(reverse("owner_inquiry_detail", args=[open_inquiry.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("owner_inquiry_detail", args=[other_inquiry.pk])).status_code, 404)
