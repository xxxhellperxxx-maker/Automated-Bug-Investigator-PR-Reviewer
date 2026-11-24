const { averagePrice, applyDiscount } = require("../src/cart");

test("average price of two items", () => {
  expect(averagePrice([{ price: 10 }, { price: 20 }])).toBe(15);
});

test("average price of an empty cart is 0", () => {
  expect(averagePrice([])).toBe(0);
});

test("discount", () => {
  expect(applyDiscount(100, 10)).toBe(90);
});
