def calculate_average(marks):

    if not marks:
        return 0

    total = sum(marks)

    average = total / len(marks)

    return round(average, 2)