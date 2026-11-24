# demo-js — JavaScript + Jest

Баг: `averagePrice([])` возвращает `NaN` вместо `0` (`src/cart.js`). Его ловит существующий тест
`tests/cart.test.js` → агент воспроизводит баг тестами, исправляет и проверяет.

Пример жалобы: «averagePrice возвращает NaN для пустой корзины вместо 0».
