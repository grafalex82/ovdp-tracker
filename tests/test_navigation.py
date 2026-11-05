"""Verify shared section navigation and canonical breadcrumbs on the available pages."""

import re

import pytest


def navigation(html, label):
    return html.split(f'<nav aria-label="{label}"', 1)[1].split('</nav>', 1)[0]


@pytest.mark.parametrize("page, current", [
    ("/", "Портфель"), ("/history", "Закрытые позиции"),
    ("/purchases/new", "Добавить облигацию"), ("purchase", "Покупка №"), ("issue", "ISIN"),
])
def test_available_pages_share_sections_and_current_breadcrumb(app, lot, page, current):
    # Open each page directly without relying on earlier navigation state.
    path = f'/purchases/{lot["id"]}' if page == "purchase" else page
    current = f'{current}{lot["id"]}' if page == "purchase" else current
    if page == "issue":
        path = f'/issues/{lot["isin"]}'
        current = lot["isin"]
    with app.test_client() as client:
        response = client.get(path)
    html = response.get_data(as_text=True)
    sections = navigation(html, "Основные разделы")
    crumbs = navigation(html, "Хлебные крошки")

    # Keep purchases, history and the add form within the highlighted portfolio section.
    assert response.status_code == 200
    assert re.search(r'<(?:a|span)\b[^>]*aria-current="(?:page|true)"[^>]*>Портфель</', sections)
    assert '<a href="/payments">Выплаты</a>' in sections
    assert '<span aria-disabled="true">Статистика</span>' in sections
    assert "Скоро" not in sections
    assert "Добавить облигацию" not in sections
    assert "Закрытые позиции" not in sections
    assert f'<span aria-current="page">{current}</span>' in crumbs
    assert crumbs.count('aria-current="page"') == 1
    assert f'>{current}</a>' not in crumbs
    if path != "/":
        assert '<a href="/">Портфель</a>' in crumbs


@pytest.mark.parametrize("closed", [False, True])
def test_purchase_breadcrumbs_ignore_referrer_and_history_state(app, database, lot, closed):
    # Close only the selected purchase when checking the closed history path.
    if closed:
        database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1000", quantity=lot["quantity"])

    # Open the same card directly and with unrelated or history referrers.
    with app.test_client() as client:
        path = f'/purchases/{lot["id"]}'
        direct = client.get(path).get_data(as_text=True)
        for referrer in ("/history", "/payments", "/statistics"):
            reached = client.get(path, headers={"Referer": f"http://localhost{referrer}"}).get_data(as_text=True)
            assert navigation(reached, "Хлебные крошки") == navigation(direct, "Хлебные крошки")
            assert navigation(reached, "Основные разделы") == navigation(direct, "Основные разделы")

    # Resolve the canonical ISIN parent even when the purchase was opened directly or is closed.
    crumbs = navigation(direct, "Хлебные крошки")
    assert f'<a href="/issues/{lot["isin"]}">{lot["isin"]}</a>' in crumbs
    assert crumbs.index(">Портфель</a>") < crumbs.index(f'>{lot["isin"]}</a>') < crumbs.index(f'Покупка №{lot["id"]}')


def test_visible_page_links_resolve_without_writing_data(app, database, lot):
    # Supply the complete issue calendar required by the shared correction form.
    event = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                           planned_date=lot["maturity_date"], amount=lot["nominal"])

    # Follow every rendered link on the currently available screens, including their parent links.
    before = database.list_lots()
    with app.test_client() as client:
        for path in ("/", "/history", "/purchases/new", f'/issues/{lot["isin"]}', f'/purchases/{lot["id"]}'):
            html = client.get(path).get_data(as_text=True)
            for href in re.findall(r'<a\b[^>]*href="([^"]+)"', html):
                assert href.startswith("/") and href != "#"
                assert client.get(href).status_code == 200

    # Navigation must not add or alter purchases, shared calendars or operations.
    assert database.list_lots() == before
    assert database.list_scheduled_payments() == [event]
    assert database.list_actual_payments() == []
    assert database.list_sales() == []


def test_rejected_form_keeps_shared_navigation_and_populated_fields(app, database):
    # Reject an incomplete submission without losing its input or its place in the portfolio.
    with app.test_client() as client:
        response = client.post("/purchases/new", data={"isin": "UA4000000001", "quantity": "0"})
    html = response.get_data(as_text=True)

    # Preserve both the canonical parent link and a non-clickable current form breadcrumb.
    assert response.status_code == 400
    assert 'value="UA4000000001"' in html
    assert 'value="0"' in html
    assert '<a href="/">Портфель</a>' in navigation(html, "Хлебные крошки")
    assert '<span aria-current="page">Добавить облигацию</span>' in navigation(html, "Хлебные крошки")
    assert 'aria-current="true">Портфель</a>' in navigation(html, "Основные разделы")
    assert database.list_lots() == []
