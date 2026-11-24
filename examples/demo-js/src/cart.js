function averagePrice(items) {
  const total = items.reduce((sum, item) => sum + item.price, 0);
  return total / items.length;
}

function applyDiscount(total, percent) {
  return Math.round(total * (100 - percent)) / 100;
}

module.exports = { averagePrice, applyDiscount };
