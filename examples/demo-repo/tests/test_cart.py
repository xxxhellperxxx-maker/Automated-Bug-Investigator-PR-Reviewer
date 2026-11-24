from app.cart import apply_discount, average_price


def test_average_price():
    assert average_price([{"price": 10}, {"price": 20}]) == 15


def test_average_price_empty_cart():
    assert average_price([]) == 0


def test_apply_known_discount():
    assert apply_discount(100, "SALE10") == 90


def test_apply_without_code():
    assert apply_discount(100, None) == 100


def test_unknown_discount_code_keeps_total():
    assert apply_discount(100, "UNKNOWN") == 100
