import json
from io import StringIO
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, TransactionTestCase

from .models import Agency, AgencyMembership, Client, Deal, Lead, Property
from .services.crm_analytics_audit import audit_report
from .services.crm_analytics_service import resolve_scope
from .agency_access import membership_for


class AnalyticsAuditTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("audit-founder")
        self.agent = User.objects.create_user("audit-agent")
        self.outsider = User.objects.create_user("audit-outsider")
        self.agency = Agency.objects.create(name="Агентство", owner=self.owner)
        AgencyMembership.objects.create(agency=self.agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=self.agency, user=self.agent, role="agent")
        property = Property.objects.create(owner=self.owner, agency=self.agency, title="Секретный адрес")
        client = Client.objects.create(owner=self.owner, agency=self.agency, name="Секретный клиент", phone="+79990000008")
        Lead.objects.create(property=property, client=client, assigned_to=self.owner, name="Клиент", phone=client.phone)
        Lead.objects.create(property=property, client=client, assigned_to=self.agent, name="Клиент", phone=client.phone)
        Deal.objects.create(owner=self.owner, agency=self.agency, responsible=self.agent, client=client, property=property)
        private = Property.objects.create(owner=self.owner, title="Личный объект")
        Lead.objects.create(property=private, name="Личный", phone="+79990000007")

    def audit(self, user, **options):
        output = StringIO()
        call_command("audit_crm_analytics", user_id=user.pk, allow_sqlite=True, stdout=output, **options)
        return json.loads(output.getvalue()), output.getvalue()

    def test_real_list_counts_match_and_output_has_no_contact_details(self):
        personal, _ = self.audit(self.owner, scope="personal")
        team, raw = self.audit(self.owner, scope="team")
        self.assertEqual(personal["comparisons"]["leads_received"]["crm_list"], 1)
        self.assertEqual(team["comparisons"]["leads_received"]["crm_list"], 2)
        self.assertEqual(team["comparisons"]["deal_stage_new"]["crm_list"], 1)
        self.assertEqual(team["mismatches"], [])
        self.assertGreater(team["performance"]["analytics_queries"], 0)
        self.assertGreater(team["performance"]["page_queries"], 0)
        for sensitive in ("Секретный адрес", "Секретный клиент", "+79990000008"):
            self.assertNotIn(sensitive, raw)

    def test_forged_member_and_team_scope_do_not_expose_other_people(self):
        agent, _ = self.audit(self.agent, scope="team", member_id=self.owner.pk)
        self.assertEqual(agent["scope"]["member_id"], self.agent.pk)
        self.assertEqual(agent["comparisons"]["leads_received"]["crm_list"], 1)
        self.assertEqual(agent["comparisons"]["property_lead_total"]["crm_list"], 1)
        manager_filter, _ = self.audit(self.owner, scope="team", member_id=self.agent.pk)
        self.assertEqual(manager_filter["scope"]["member_id"], self.agent.pk)
        self.assertEqual(manager_filter["mismatches"], [])
        with self.assertRaises(CommandError):
            self.audit(self.outsider, scope="team")

    def test_postgresql_is_required_for_pilot_claim(self):
        if connection.vendor == "sqlite":
            with self.assertRaises(CommandError):
                call_command("audit_crm_analytics", user_id=self.owner.pk, stdout=StringIO())

    def test_report_query_count_does_not_grow_with_lead_rows(self):
        scope = resolve_scope(self.owner, requested_scope="team", membership=membership_for(self.owner))
        before = audit_report(self.owner, scope)
        property = Property.objects.get(agency=self.agency)
        Lead.objects.bulk_create([
            Lead(property=property, owner=property.owner, agency=property.agency,
                 assigned_to=self.owner, name="Массовая проверка", phone="+79990000000")
            for _ in range(40)
        ])
        after = audit_report(self.owner, scope)
        self.assertEqual(after["mismatches"], [])
        self.assertEqual(after["comparisons"]["leads_received"]["analytics"], 42)
        self.assertLessEqual(after["performance"]["analytics_queries"], before["performance"]["analytics_queries"] + 2)
        self.assertLessEqual(after["performance"]["page_queries"], before["performance"]["page_queries"] + 2)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL")
class PostgreSQLAnalyticsAuditTests(TransactionTestCase):
    def test_postgresql_read_only_snapshot_matches_real_lists(self):
        user = User.objects.create_user("pg-audit-owner")
        property = Property.objects.create(owner=user, title="PG объект")
        Lead.objects.create(property=property, name="Клиент", phone="+79990000003")
        output = StringIO()
        call_command("audit_crm_analytics", user_id=user.pk, scope="personal", stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual(result["database"], "postgresql")
        self.assertEqual(result["mismatches"], [])
