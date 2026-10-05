from django.core.paginator import Paginator


def paged_board_columns(request, *, choices, queryset, field, hydrate, anchor_prefix, page_size=12):
    """Page each board lane in SQL, then load details for just its visible cards."""
    columns = []
    for key, label in choices:
        parameter = f"p_{key}"
        ids = queryset.filter(**{field: key}).order_by("-updated_at", "-pk").values_list("pk", flat=True)
        page = Paginator(ids, page_size).get_page(request.GET.get(parameter))
        visible_ids = list(page.object_list)
        cards = hydrate(visible_ids) if visible_ids else []
        by_id = {card.pk: card for card in cards}
        page.object_list = [by_id[pk] for pk in visible_ids]

        def page_url(number):
            query = request.GET.copy()
            if number == 1:
                query.pop(parameter, None)
            else:
                query[parameter] = str(number)
            encoded = query.urlencode()
            path = f"{request.path}?{encoded}" if encoded else request.path
            return f"{path}#{anchor_prefix}-{key}"

        columns.append({
            "key": key,
            "label": label,
            "page": page,
            "count": page.paginator.count,
            "previous_url": page_url(page.previous_page_number()) if page.has_previous() else None,
            "next_url": page_url(page.next_page_number()) if page.has_next() else None,
        })
    return columns
