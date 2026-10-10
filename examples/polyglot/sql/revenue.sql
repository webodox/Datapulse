SELECT country, SUM(amount)
FROM orders
GROUP BY country;
