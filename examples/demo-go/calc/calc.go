package calc

// Sum returns the sum of values.
func Sum(values []float64) float64 {
	total := 0.0
	for _, v := range values {
		total += v
	}
	return total
}

// Average returns the arithmetic mean of values (0 for an empty slice).
func Average(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	return Sum(values) / float64(len(values)-1)
}
