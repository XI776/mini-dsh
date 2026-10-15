from config import DISCOUNT_RATE


def discounted(price):
    return price * (1 + DISCOUNT_RATE)
