from decimal import Decimal
from xml.etree import ElementTree as ET


class AvitoExportService:
    """Builds a downloadable Avito XML v3 feed for one realtor."""

    TYPE_MAPPING = {
        "apartment": ("Квартиры", "Квартира"),
        "house": ("Дома, дачи, коттеджи", "Дом"),
        "cottage": ("Дома, дачи, коттеджи", "Дача"),
        "villa": ("Дома, дачи, коттеджи", "Коттедж"),
        "land": ("Земельные участки", "Земельный участок"),
        "office": ("Коммерческая недвижимость", "Офис"),
        "commercial": ("Коммерческая недвижимость", "Коммерческая недвижимость"),
    }

    @classmethod
    def validation_errors(cls, property, profile):
        errors = []
        if property.status != "published":
            errors.append("объект не опубликован")
        if not property.price:
            errors.append("не указана цена")
        elif property.currency != "RUB":
            errors.append("цена для Avito должна быть в рублях")
        if not property.address:
            errors.append("не указан адрес")
        if not property.description:
            errors.append("не добавлено полное описание")
        if not property.images.exists():
            errors.append("не добавлена хотя бы одна фотография")
        if not profile or not profile.phone:
            errors.append("в личном кабинете не указан телефон риелтора")
        if property.property_type not in cls.TYPE_MAPPING:
            errors.append("не поддерживается тип недвижимости")
        return errors

    @classmethod
    def get_exportable(cls, properties, profile):
        valid, invalid = [], []
        for property in properties:
            errors = cls.validation_errors(property, profile)
            if errors:
                invalid.append((property, errors))
            else:
                valid.append(property)
        return valid, invalid

    @classmethod
    def build_xml(cls, properties, profile, base_url):
        root = ET.Element("Ads", {"formatVersion": "3", "target": "Avito.ru"})
        for property in properties:
            category, object_type = cls.TYPE_MAPPING[property.property_type]
            ad = ET.SubElement(root, "Ad")
            cls._add(ad, "Id", f"realestate-ai-{property.pk}")
            cls._add(ad, "Category", category)
            cls._add(ad, "OperationType", "Продам" if property.avito_operation == "sell" else "Сдам")
            cls._add(ad, "PropertyRights", "Посредник")
            cls._add(ad, "ObjectType", object_type)
            cls._add(ad, "Title", property.marketing_headline or property.title)
            cls._add(ad, "Description", property.description)
            cls._add(ad, "Price", str(int(Decimal(property.price))))
            cls._add(ad, "Address", property.address)
            cls._add(ad, "ContactPhone", profile.phone)
            if property.area:
                cls._add(ad, "Square", str(property.area))
            if property.rooms:
                cls._add(ad, "Rooms", str(property.rooms))
            if property.floor:
                cls._add(ad, "Floor", str(property.floor))
            if property.floors_total:
                cls._add(ad, "Floors", str(property.floors_total))
            if property.land_area:
                cls._add(ad, "LandArea", str(property.land_area))
            images = ET.SubElement(ad, "Images")
            for image in property.images.all():
                ET.SubElement(images, "Image", {"url": f"{base_url}{image.image.url}"})
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _add(parent, tag, value):
        element = ET.SubElement(parent, tag)
        element.text = str(value)
