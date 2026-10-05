from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from billing.models import BillingAccount
from billing.reservations import reserved_resources
from billing.rules import Operation
from billing.services import AccountService

from ..models import AIContent, Client, ClientInteraction, ClientReminder, Deal, Lead, Property, PropertyImage, RealtorProfile, Showing


class DemoDataService:
    """Creates a safe, clearly marked walkthrough workspace for one user."""

    @classmethod
    @transaction.atomic
    def create_for_user(cls, user):
        account = AccountService.ensure_personal(user)
        BillingAccount.objects.select_for_update().get(pk=account.pk)
        profile, _ = RealtorProfile.objects.select_for_update().get_or_create(user=user)
        if profile.demo_data_created:
            return None
        filenames = ("demo-living-room.jpg", "demo-kitchen.jpg", "demo-bedroom.jpg")
        size = sum((Path(settings.BASE_DIR) / "static" / "onboarding" / filename).stat().st_size for filename in filenames)
        with reserved_resources(account=account, actor=user, action=Operation.CREATE_PROPERTY,
                amounts={"active_properties": 1, "total_properties": 1, "landings": 1, "storage_bytes": size}):
            return cls._create_for_user_locked(user, profile)

    @classmethod
    def _create_for_user_locked(cls, user, profile):

        property = Property.objects.create(
            owner=user,
            title="Демо · квартира у парка",
            marketing_headline="Светлая квартира у парка — пример карточки",
            property_type="apartment",
            deal_type="sale",
            status="published",
            price=285000,
            currency="EUR",
            address="Лиссабон, район Алваладе",
            area=78,
            rooms=3,
            bathrooms=2,
            floor=4,
            floors_total=8,
            year_built=2021,
            condition="excellent",
            amenities="Балкон, лифт, парковка, кондиционер, рядом парк и метро.",
            short_description="Готовый пример объекта: от карточки до опубликованного лендинга.",
            description="Светлая трёхкомнатная квартира в спокойном районе рядом с парком. Используйте этот пример, чтобы посмотреть, как заполняются характеристики, тексты и лендинг.",
            landing_published=True,
            landing_template="modern",
            landing_accent="emerald",
            landing_title="Квартира у парка для комфортной жизни",
            landing_subtitle="Пример готового лендинга, который можно открыть, изменить или удалить.",
            landing_about_title="Пространство для жизни",
            landing_contact_title="Запросить просмотр",
            landing_trust_about="Демо-профиль показывает, как выглядит доверительный блок риелтора.",
            seo_title="Демо-квартира у парка",
            seo_description="Пример заполненного объекта для знакомства с Rieltor AI.",
            is_demo=True,
        )
        demo_images = [
            "demo-living-room.jpg",
            "demo-kitchen.jpg",
            "demo-bedroom.jpg",
        ]
        for order, filename in enumerate(demo_images):
            image_path = Path(settings.BASE_DIR) / "static" / "onboarding" / filename
            with image_path.open("rb") as image_file:
                PropertyImage.objects.create(
                    property=property,
                    image=File(image_file, name=f"demo-{filename}"),
                    order=order,
                    is_primary=order == 0,
                )
        first_client = Client.objects.create(
            owner=user,
            name="Анна Демонстрационная",
            phone="+70000000001",
            source="landing",
            preferred_area="Алваладе",
            budget=300000,
            notes="Это демо-клиент. На нём можно безопасно посмотреть карточку и историю.",
            status="in_progress",
            is_demo=True,
        )
        first_lead = Lead.objects.create(
            property=property,
            client=first_client,
            name=first_client.name,
            phone=first_client.phone,
            contact_purpose="viewing",
            message="Хочу посмотреть квартиру на этой неделе.",
            status="read",
            interest_type="buy",
            is_demo=True,
        )
        ClientInteraction.objects.create(
            client=first_client,
            lead=first_lead,
            interaction_type="message",
            text="Демо: клиенту отправлен ответ с предложением времени для просмотра.",
        )
        deal = Deal.objects.create(
            owner=user,
            responsible=user,
            client=first_client,
            property=property,
            lead=first_lead,
            stage="viewing",
            expected_commission=8500,
            is_demo=True,
        )
        ClientReminder.objects.create(
            owner=user,
            client=first_client,
            deal=deal,
            text="Демо: подтвердить время просмотра",
            due_at=timezone.now() + timedelta(days=1),
        )
        Showing.objects.create(
            owner=user,
            deal=deal,
            starts_at=timezone.now() + timedelta(days=2),
            location=property.address,
        )
        second_client = Client.objects.create(
            owner=user,
            name="Илья Демонстрационный",
            phone="+70000000002",
            source="landing",
            status="new",
            is_demo=True,
        )
        Lead.objects.create(
            property=property,
            client=second_client,
            name=second_client.name,
            phone=second_client.phone,
            contact_purpose="presentation",
            message="Пришлите, пожалуйста, презентацию объекта.",
            status="new",
            interest_type="buy",
            is_demo=True,
        )
        AIContent.objects.create(
            property=property,
            content_type="headline",
            tone="business",
            title=property.title,
            content="Просторная квартира у парка: комфортный район и продуманная планировка.",
            is_applied=True,
            apply_target="marketing_headline",
        )
        profile.demo_data_created = True
        profile.onboarding_started = True
        profile.save(update_fields=["demo_data_created", "onboarding_started"])
        return property
