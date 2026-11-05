"""Translate validation messages at the user interface boundary."""

from .quantity import MAX_BOND_QUANTITY

_MESSAGES = {
    "The saved ISIN cannot be changed.": "ISIN сохранённого выпуска нельзя изменить.",
    "Cannot remove an event with a confirmed payment.":
        "Нельзя удалить или перенести дату события с подтверждённой выплатой любой покупки этого ISIN.",
    "Could not update the issue. Retry the operation.":
        "Не удалось сохранить облигацию. Изменения не сохранены; повторите попытку.",
    "This field is required.": "Заполните это поле.",
    "Currency must be UAH, USD, or EUR.": "Выберите валюту UAH, USD или EUR.",
    "Amount must be positive.": "Сумма должна быть больше нуля.",
    "Amount must be nonnegative.": "Сумма не может быть отрицательной.",
    "Money must be a finite decimal string with a dot or comma separator.":
        "Введите число с точкой или запятой в качестве разделителя.",
    "Money must be a finite decimal string.": "Введите корректную денежную сумму.",
    "Money must be finite.": "Введите конечную денежную сумму.",
    "Money must have at most two decimal places.": "Укажите не более двух знаков после разделителя.",
    "Enter a valid date in YYYY-MM-DD format.": "Введите корректную дату в формате ГГГГ-ММ-ДД.",
    "Coupons must be a list of date and amount objects.": "Укажите список купонов с датами и суммами.",
    "Each coupon must contain date and amount strings.": "Для каждого купона укажите дату и сумму.",
    "Issue date cannot be after maturity.": "Дата выпуска не может быть позже даты погашения.",
    "Coupon dates must be unique.": "Даты купонов не должны повторяться.",
    "Coupon dates must not be after maturity.": "Даты купонов не могут быть позже даты погашения.",
    "Nominal rate must be a finite nonnegative percentage.": "Укажите неотрицательную номинальную ставку.",
    "Coupon period must be a nonnegative whole number of days.": "Купонный период должен быть целым числом дней.",
    "Unsupported payment type.": "Этот тип выплаты не поддерживается.",
    "Exactly one redemption matching maturity is required.": "Нужно одно погашение на дату погашения выпуска.",
    "Quantity must be a positive integer within the supported range.":
        "Количество должно быть положительным целым числом.",
    "Load issue data or explicitly choose manual entry before saving.":
        "Загрузите данные выпуска или выберите ручной ввод перед сохранением.",
    "Loaded data belongs to a different ISIN. Load the current issue again.":
        "Загруженные данные относятся к другому ISIN. Загрузите текущий выпуск заново.",
    "Purchase date cannot be in the future; planned purchases are not supported.":
        "Дата покупки не может быть в будущем; плановые покупки пока не поддерживаются.",
    "Maturity date cannot be before the purchase date.": "Дата погашения не может быть раньше даты покупки.",
    "Saved issue not found. Load NBU data or choose manual entry.":
        "Сохранённый выпуск не найден. Загрузите данные НБУ или выберите ручной ввод.",
    "This field conflicts with the existing ISIN.": "Значение отличается от сохранённых данных этого ISIN.",
    "Calendar dates or amounts conflict with the existing ISIN.":
        "Даты или суммы календаря отличаются от сохранённых данных этого ISIN.",
    "Enter a valid JSON coupon array.": "Укажите корректный список купонов.",
    "Reload NBU data before saving this purchase.": "Перед сохранением покупки загрузите данные НБУ повторно.",
    "Loaded data belongs to a different ISIN. Reload the issue.":
        "Загруженные данные относятся к другому ISIN. Загрузите выпуск повторно.",
    "Loaded NBU data conflicts with the saved ISIN. Reload the issue.":
        "Данные НБУ отличаются от сохранённых данных этого ISIN. Загрузите выпуск повторно.",
    "Enter a valid sale date.": "Введите корректную дату продажи.",
    "Sale date cannot be in the future.": "Дата продажи не может быть в будущем.",
    "Quantity must be a positive integer.": "Количество должно быть положительным целым числом.",
    f"Quantity must not exceed {MAX_BOND_QUANTITY} bonds.":
        f"Количество должно быть не больше {MAX_BOND_QUANTITY:,} облигаций.".replace(",", " "),
    "Enter a positive sale price with at most two decimal places.":
        "Введите положительную цену с не более чем двумя знаками после разделителя.",
    "Purchase not found.": "Покупка не найдена.",
    "Payment is not due for this purchase.": "Эта выплата не причитается выбранной покупке.",
    "Payment does not require confirmation.": "Эта выплата не требует подтверждения.",
    "Actual payment date cannot precede the purchase.": "Фактическая дата выплаты не может быть раньше покупки.",
    "Actual payment date cannot be in the future.": "Фактическая дата выплаты не может быть в будущем.",
    "Could not confirm the payment. Retry the operation.":
        "Не удалось подтвердить выплату. Изменения не сохранены; повторите попытку.",
    "Confirmed payment not found.": "Подтверждённая выплата не найдена.",
    "Could not update the payment. Retry the operation.":
        "Не удалось исправить выплату. Изменения не сохранены; повторите попытку.",
    "Sale not found.": "Продажа не найдена.",
    "Confirm deletion of the saved sale.": "Подтвердите удаление сохранённой продажи.",
    "Could not delete the sale. Retry the operation.":
        "Не удалось удалить продажу. Изменения не сохранены; повторите попытку.",
    "Confirm deletion of the complete purchase history.": "Подтвердите удаление всей истории покупки.",
    "Could not delete the purchase. Retry the operation.":
        "Не удалось удалить покупку. Изменения не сохранены; повторите попытку.",
    "Sale date cannot precede the purchase.": "Дата продажи не может быть раньше даты покупки.",
    "Cannot sell bonds after their actual redemption.": "Нельзя продать облигации после фактического погашения.",
    "Sale exceeds the available quantity in the purchase history.":
        "Количество продажи превышает доступный остаток с учётом истории покупки.",
    "The change conflicts with a confirmed payment.": "Изменение противоречит подтверждённой выплате.",
    "Shared issue fields cannot be edited here.": "Общие реквизиты выпуска нельзя изменить в этой форме.",
    "Unsupported portfolio currency.": "Эта валюта портфеля не поддерживается.",
    "Position not found.": "Позиция не найдена.",
    "Enter a complete 12-character ISIN.": "Введите полный ISIN из 12 символов.",
    "NBU returned multiple records for this ISIN.": "НБУ вернул несколько записей для этого ISIN.",
    "Could not load the NBU directory. Retry or choose manual entry.":
        "Не удалось загрузить справочник НБУ. Повторите попытку или выберите ручной ввод.",
}


def user_message(message: str) -> str:
    """Return the Russian text for a known internal message or a safe generic error."""
    return _MESSAGES.get(message, "Не удалось выполнить операцию. Проверьте введённые данные и повторите попытку.")
