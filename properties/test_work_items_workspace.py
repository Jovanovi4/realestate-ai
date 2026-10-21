from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Client, ClientReminder, Deal, Lead, OwnerInquiry, Property, Showing


class WorkItemsWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('work-table-user')
        cls.other = User.objects.create_user('work-table-other')
        cls.now = timezone.make_aware(datetime(2026, 10, 22, 12))
        cls.contact = Client.objects.create(owner=cls.user, name='Анна', phone='+79991234567', contact_role='both')
        cls.property = Property.objects.create(owner=cls.user, title='Квартира у парка', address='Лесная, 8')
        cls.deal = Deal.objects.create(owner=cls.user, responsible=cls.user, client=cls.contact, property=cls.property)
        cls.task = ClientReminder.objects.create(owner=cls.user, client=cls.contact, text='Уточнить бюджет', due_at=cls.now-timedelta(days=1))
        cls.future = ClientReminder.objects.create(owner=cls.user, property=cls.property, text='Собрать документы', due_at=cls.now+timedelta(days=1))
        cls.undated = ClientReminder.objects.create(owner=cls.user, text='Общая задача без даты')
        cls.lead = Lead.objects.create(property=cls.property, client=cls.contact, name='Анна', phone=cls.contact.phone,
            first_contacted_at=cls.now-timedelta(hours=1), next_action='Отправить подборку', next_action_due_at=cls.now+timedelta(hours=1))
        cls.inquiry = OwnerInquiry.objects.create(owner=cls.user, assigned_to=cls.user, client=cls.contact, deal_type='sale',
            address='Набережная, 7', first_contacted_at=cls.now-timedelta(days=1), next_action='Обсудить условия', next_action_due_at=cls.now-timedelta(hours=2))
        cls.showing = Showing.objects.create(owner=cls.user, deal=cls.deal, starts_at=cls.now+timedelta(hours=3), location='У подъезда')
        cls.private_task = ClientReminder.objects.create(owner=cls.other, text='Чужая приватная задача', due_at=cls.now)
        ClientReminder.objects.create(owner=cls.user, text='Завершённое дело', is_done=True)
        ClientReminder.objects.create(owner=cls.user, text='Отменённое дело', cancelled_at=cls.now)

    def setUp(self):
        self.client.force_login(self.user)
        patcher = patch('properties.workday_views.timezone.now', return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def items(self, response):
        self.assertEqual(response.status_code, 200)
        return {item['key']: item for item in response.context['work_items']}

    def test_today_includes_overdue_and_counters_cover_all_open_periods(self):
        response = self.client.get(reverse('workday'))
        self.assertEqual(set(self.items(response)), {f'task:{self.task.pk}', f'lead:{self.lead.pk}', f'owner:{self.inquiry.pk}', f'showing:{self.showing.pk}'})
        counts = {tab['value']: tab['count'] for tab in response.context['work_period_tabs']}
        self.assertEqual(counts, {'all': 6, 'today': 4, 'overdue': 2, 'future': 1, 'unscheduled': 1})
        for period, keys in [('future', {f'task:{self.future.pk}'}), ('unscheduled', {f'task:{self.undated.pk}'}),
                             ('overdue', {f'task:{self.task.pk}', f'owner:{self.inquiry.pk}'})]:
            with self.subTest(period=period):
                self.assertEqual(set(self.items(self.client.get(reverse('work_items'), {'period': period}))), keys)

    def test_search_and_kind_preserve_period_and_do_not_expose_other_users(self):
        response = self.client.get(reverse('work_items'), {'q': 'Лесная', 'kind': 'task', 'period': 'future'}, HTTP_HX_REQUEST='true')
        self.assertEqual(set(self.items(response)), {f'task:{self.future.pk}'})
        self.assertContains(response, 'id="work-workspace"')
        self.assertNotContains(response, '<html')
        self.assertIn('HX-Request', response.headers['Vary'])
        item = response.context['work_items'][0]
        query = parse_qs(urlparse(item['panel_url']).query)
        self.assertEqual(query['q'], ['Лесная'])
        self.assertEqual(query['kind'], ['task'])
        self.assertEqual(query['period'], ['future'])
        response = self.client.get(reverse('work_items'), {'q': 'Чужая', 'item': f'task:{self.private_task.pk}'})
        self.assertEqual(self.items(response), {})
        self.assertIsNone(response.context['selected_item'])
        self.assertNotContains(response, self.private_task.text)
        response = self.client.get(reverse('work_items'), {'q': self.contact.phone})
        self.assertEqual(set(self.items(response)), {f'task:{self.task.pk}', f'lead:{self.lead.pk}', f'owner:{self.inquiry.pk}', f'showing:{self.showing.pk}'})

    def test_each_type_opens_a_panel_with_its_own_action_and_return_link(self):
        for kind, instance in [('task', self.task), ('lead', self.lead), ('owner', self.inquiry), ('showing', self.showing)]:
            with self.subTest(kind=kind):
                response = self.client.get(reverse('work_items'), {'item': f'{kind}:{instance.pk}'}, HTTP_HX_REQUEST='true')
                selected = response.context['selected_item']
                self.assertEqual(selected['key'], f'{kind}:{instance.pk}')
                self.assertContains(response, 'id="work-panel"')
                self.assertContains(response, 'data-work-panel-close')
                self.assertEqual(selected['can_complete'], kind == 'task')
                if kind in {'task', 'showing'}:
                    self.assertEqual(parse_qs(urlparse(selected['detail_url']).query)['next'], [response.context['work_list_url']])
        self.assertIn('?complete=1', self.client.get(reverse('work_items'), {'item': f'lead:{self.lead.pk}'}).context['selected_item']['action_url'])

    def test_filtered_out_or_completed_item_cannot_reappear_in_panel(self):
        response = self.client.get(reverse('work_items'), {'period': 'future', 'item': f'task:{self.task.pk}'})
        self.assertIsNone(response.context['selected_item'])
        self.task.is_done = True
        self.task.save(update_fields=['is_done'])
        response = self.client.get(reverse('work_items'), {'item': f'task:{self.task.pk}'})
        self.assertIsNone(response.context['selected_item'])
        self.assertNotContains(response, self.task.text)

    def test_checkbox_keys_distinguish_kinds_and_search_is_outside_filters(self):
        response = self.client.get(reverse('work_items'))
        items = self.items(response)
        self.assertEqual(len(items), 6)
        html = response.content.decode()
        self.assertLess(html.index('id="work-search-query"'), html.index('data-work-filter-toggle'))
        self.assertLess(html.index('data-work-filter-toggle'), html.index('id="work-filter-panel"'))
        for key in items:
            self.assertContains(response, f'name="work_keys" value="{key}"', count=2)
        self.assertContains(response, 'data-work-select-all', count=2)

    def test_owner_feedback_task_requires_its_result_workflow(self):
        feedback = ClientReminder.objects.create(owner=self.user, client=self.contact, owner_showing=self.showing, property=self.property,
            text='Сообщить собственнику об итогах', due_at=self.now+timedelta(hours=4))
        response = self.client.get(reverse('work_items'), {'item': f'task:{feedback.pk}'})
        selected = response.context['selected_item']
        self.assertFalse(selected['can_complete'])
        self.assertEqual(selected['action_label'], 'Записать итог')
        self.assertNotContains(response, f'data-work-complete="task:{feedback.pk}"')
