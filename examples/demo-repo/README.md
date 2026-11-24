# bug-demo

Демо-репозиторий для AI Bug Investigator.

**Баг 1 (активен):** `average_price([])` — деление на ноль в `app/cart.py`, строка 6. Тест `test_average_price_empty_cart` падает.

**Баг 2 (включить после мержа PR с исправлением бага 1):** в `app/cart.py`, строка 12, замените
`rate = DISCOUNTS.get(code, 0)` на `rate = DISCOUNTS[code]` и закоммитьте в `main`. Тест
`test_unknown_discount_code_keeps_total` начнёт падать с `KeyError`.

Одновременно должен быть активен только один баг: агент проверяет исправление по зелёным тестам,
и чужой падающий тест помешает ему.

GitHub Actions запускает тесты на каждый push и pull request.
