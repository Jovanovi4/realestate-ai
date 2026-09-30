from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Agency, AgencyMembership, Client


class ClientImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("csv-owner", password="password")
        self.client.force_login(self.owner)
        self.url = reverse("client_import")

    def upload(self, text, *, encoding="utf-8-sig"):
        file = SimpleUploadedFile("clients.csv", text.encode(encoding), content_type="text/csv")
        return self.client.post(self.url, {"file": file})

    def test_imports_export_columns_and_skips_existing_or_repeated_phone(self):
        Client.objects.create(owner=self.owner, name="Ранее", phone="89260000001")
        response = self.upload(
            'Имя,Телефон,Статус,Источник,Бюджет,Район,Удобное время,Создан\n'
            'Повтор,+79260000001,Новый,Другое,,, ,\n'
            'Анна,8 (926) 000-00-02,В работе,Рекомендация,12500000.50,Центр,После 18:00,\n'
            'Анна снова,+79260000002,Новый,Другое,,,,\n'
        )
        self.assertRedirects(response, reverse("client_list"))
        self.assertContains(self.client.get(reverse("client_list")), "Анна")
        self.assertEqual(Client.objects.count(), 2)
        imported = Client.objects.get(name="Анна")
        self.assertEqual(imported.phone, "+79260000002")
        self.assertEqual(imported.status, "in_progress")
        self.assertEqual(imported.source, "recommendation")
        self.assertEqual(str(imported.budget), "12500000.50")
        self.assertEqual(imported.preferred_area, "Центр")

    def test_bad_row_rejects_entire_file(self):
        response = self.upload("Имя;Телефон;Бюджет\nАнна;+79260000002;1000\nБорис;не номер;2000\n", encoding="cp1251")
        self.assertContains(response, "Строка 3")
        self.assertEqual(Client.objects.count(), 0)

    def test_exported_csv_can_be_imported_to_another_workspace(self):
        Client.objects.create(
            owner=self.owner, name="Клиент экспорта", phone="+79260000006",
            source="avito", status="viewing", preferred_area="Центр, рядом с парком",
        )
        exported = self.client.get(reverse("client_export")).content
        target = User.objects.create_user("csv-target", password="password")
        self.client.force_login(target)

        response = self.client.post(self.url, {
            "file": SimpleUploadedFile("export.csv", exported, content_type="text/csv"),
        })

        self.assertRedirects(response, reverse("client_list"))
        imported = Client.objects.get(owner=target)
        self.assertEqual(imported.name, "Клиент экспорта")
        self.assertEqual(imported.phone, "+79260000006")
        self.assertEqual(imported.source, "avito")
        self.assertEqual(imported.status, "viewing")
        self.assertEqual(imported.preferred_area, "Центр, рядом с парком")

    def test_import_is_isolated_by_workspace_and_repeat_is_safe(self):
        outsider = User.objects.create_user("csv-outsider", password="password")
        Client.objects.create(owner=outsider, name="Другой", phone="+79260000003")
        text = "Имя,Телефон\nАнна,+79260000003\n"
        self.assertRedirects(self.upload(text), reverse("client_list"))
        self.assertRedirects(self.upload(text), reverse("client_list"))
        self.assertEqual(Client.objects.filter(owner=self.owner).count(), 1)
        self.assertEqual(Client.objects.count(), 2)

    def test_team_member_imports_to_shared_base_and_skips_team_duplicate(self):
        member = User.objects.create_user("csv-member", password="password")
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=member, role="agent")
        Client.objects.create(owner=self.owner, agency=agency, name="Уже есть", phone="+79260000004")
        self.client.force_login(member)
        self.assertContains(self.client.get(self.url), "общую базу команды")
        response = self.upload("Имя,Телефон\nПовтор,89260000004\nНовый,+79260000005\n")
        self.assertRedirects(response, reverse("client_list"))
        imported = Client.objects.get(phone="+79260000005")
        self.assertEqual((imported.owner, imported.agency), (member, agency))
        self.assertEqual(Client.objects.filter(agency=agency).count(), 2)

    def test_file_limits_and_missing_headers_do_not_create_clients(self):
        self.assertContains(self.upload("Имя,Город\nАнна,Москва\n"), "Нужны столбцы")
        large = SimpleUploadedFile("clients.csv", b"a" * 1_000_001, content_type="text/csv")
        self.assertContains(self.client.post(self.url, {"file": large}), "Файл слишком большой")
        self.assertEqual(Client.objects.count(), 0)

    def test_login_required(self):
        self.assertContains(self.client.get(self.url), "личную базу")
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_client_header_uses_labeled_icons_for_secondary_actions(self):
        response = self.client.get(reverse("client_list"))
        for name, label, icon in (
            ("client_board", "Статусы клиентов", "kanban"),
            ("client_export", "Экспорт CSV", "download"),
            ("client_import", "Импорт CSV", "upload"),
        ):
            self.assertContains(response, f'href="{reverse(name)}" aria-label="{label}" data-tooltip="{label}"')
            self.assertContains(response, f'<use href="#ui-icon-{icon}"></use>')
