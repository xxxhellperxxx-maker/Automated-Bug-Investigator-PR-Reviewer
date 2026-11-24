DISCOUNTS = {"SALE10": 0.10, "SALE20": 0.20}


def average_price(items):
    total = sum(item["price"] for item in items)
    return total / len(items)


def apply_discount(total, code):
    if not code:
        return total
    rate = DISCOUNTS.get(code, 0)
    return round(total * (1 - rate), 2)
