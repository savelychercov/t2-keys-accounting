from dataclasses import dataclass
import gspread
from datetime import datetime
import asyncio
from prettytable import PrettyTable
from pprint import pp
import logger
import json
import random
from difflib import SequenceMatcher


# region Constants


datetime_format = "%d.%m.%Y %H:%M:%S"
credentials_path = "credentials/gspread_credentials.json"
tables_path = "credentials/spreadsheet_tables.json"
last_update_cell = (1, 8)
# mail keysspreadsheetsbot@keysspreadsheetsbot.iam.gserviceaccount.com


# endregion


# region Utils


async def find_similar(query, strings):
    def similarity(a, b):
        return SequenceMatcher(None, a, b).ratio()
    matches = [s for s in strings if query in s]
    if not matches:
        scored_matches = sorted(strings, key=lambda s: similarity(query, s), reverse=True)
        matches = [s for s in scored_matches if similarity(query, s) > 0.5]
    return matches[:5]


async def update(wks: gspread.Worksheet, cell_str: str, values: list[list]):
    await asyncio.to_thread(wks.update, cell_str, values)


async def col_values(wks: gspread.Worksheet, col: int):
    return await asyncio.to_thread(wks.col_values, col)


async def row_values(wks: gspread.Worksheet, row: int):
    return await asyncio.to_thread(wks.row_values, row)


async def get_all_values(wks: gspread.Worksheet):
    return await asyncio.to_thread(wks.get_all_values)


async def auto_resize(wks: gspread.Worksheet, start_col: int, end_col: int):
    await asyncio.to_thread(wks.columns_auto_resize, start_col, end_col)


async def add_rows(wks: gspread.Worksheet, rows_count: int):
    await asyncio.to_thread(wks.add_rows, rows_count)


async def clear(wks: gspread.Worksheet):
    print(f"WARNING: Clearing sheet {wks.title}")
    await asyncio.to_thread(wks.clear)


def sort_values_by_headers(russian_headers, values, keys_headers):
    header_to_key = swap(keys_headers)
    sorted_keys = [header_to_key[header] for header in russian_headers]
    value_dict = dict(zip(keys_headers.keys(), values))
    sorted_values = [value_dict[key] for key in sorted_keys]
    return sorted_values


def print_table(rows: list[list], headers: list[str]):
    table = PrettyTable()
    table.field_names = headers
    for row in rows:
        table.add_row(row)
    print(table)


def swap(d: dict):
    return {v: k for k, v in d.items()}


def cell(x, y):
    letters = ""
    while x > 0:
        x, remainder = divmod(x - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{y}"


def from_to(x_from, y_from, x_to, y_to):
    return f"{cell(x_from, y_from)}:{cell(x_to, y_to)}"


def singleton(cls):
    instances = {}

    def getinstance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return getinstance


# endregion


# region Connection


logger = logger.Logger()
gs = None
if gs is None:
    gs = gspread.service_account(filename=credentials_path)


spreadsheet = None
if spreadsheet is None:
    with open(tables_path, "r", encoding="utf-8") as f:
        tables_data = json.load(f)
    spreadsheet = gs.open_by_url(tables_data["spreadsheet_url"])


# endregion


# region Classes


class Entry:
    def __init__(
            self,
            key_name: str,
            emp_firstname: str,
            emp_lastname: str,
            emp_phone: str,
            time_received: datetime,
            time_returned: datetime,
            comment: str,
            row: int = None
    ):
        self.key_name = key_name
        self.emp_firstname = emp_firstname
        self.emp_lastname = emp_lastname
        self.emp_phone = emp_phone
        self.comment = comment
        self.row = row

        if isinstance(time_received, str):
            self.time_received = datetime.strptime(time_received, datetime_format)
        elif isinstance(time_received, datetime):
            self.time_received = time_received
        else:
            raise TypeError(f"time_received must be a datetime object or a string in '%d.%m.%Y %H:%M:%S' format Current value: {time_received}")

        if isinstance(time_returned, str) and not time_returned.strip() == "":
            self.time_returned = datetime.strptime(time_returned, datetime_format)
        elif isinstance(time_returned, datetime):
            self.time_returned = time_returned
        elif not time_returned:
            self.time_returned = None
        else:
            raise TypeError(f"time_returned must be a datetime object or a string in '%d.%m.%Y %H:%M:%S' format. Current value: {time_returned}")

    def __repr__(self):
        return (
            "----------\n"
            f"Key: {self.key_name}\n"
            f"Employee: {self.emp_firstname} {self.emp_lastname} ({self.emp_phone})\n"    
            f"Received: {self.time_received.strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"Returned: {self.time_returned.strftime('%d.%m.%Y %H:%M:%S') if self.time_returned else 'Not returned'}\n"
            f"Comment: {self.comment}"
            "\n----------\n"
        )


class KeysAccountingTable:
    def __init__(self):
        with open(tables_path, "r", encoding="utf-8") as f:
            td = json.load(f)
        self.wks = spreadsheet.worksheet(td["keys_accounting_wks"])
        self.keys_headers = {
            "key_name":      "Ключ",
            "emp_firstname": "Имя",
            "emp_lastname":  "Фамилия",
            "emp_phone":     "Номер телефона",
            "time_received": "Время получения",
            "time_returned": "Время сдачи",
            "comment":       "Комментарий",
        }

    async def new_entry(self, key_name: str, emp_firstname: str, emp_lastname: str, emp_phone: str, comment: str = ""):
        await self.append_entry(Entry(key_name, emp_firstname, emp_lastname, emp_phone, datetime.now(), None, comment))

    async def setup_table(self):
        await self.check_has_free_rows(1)
        await update(self.wks, cell(1, 1), [list(self.keys_headers.values())])
        await self.type_last_update()
        await auto_resize(self.wks, 1, len(self.keys_headers)+1)

    async def get_headers(self):
        return (await row_values(self.wks, 1))[0:len(self.keys_headers)]

    async def append_entry(self, entry: Entry):
        insert_row = len(await col_values(self.wks, 1)) + 1
        headers = await self.get_headers()
        values = []
        for header in headers:
            key = swap(self.keys_headers)[header]
            val = getattr(entry, key)
            if isinstance(val, datetime):
                val = val.strftime(datetime_format)
            values.append(val)
        await self.check_has_free_rows(insert_row)
        await update(self.wks, cell(1, insert_row), [values])
        await self.type_last_update()
        await auto_resize(self.wks, 1, len(headers))

    async def check_has_free_rows(self, rows_count):
        current_rows = self.wks.row_count
        if current_rows < rows_count:
            await add_rows(self.wks, rows_count - current_rows)

    async def type_last_update(self):
        text = f"Last update: {datetime.now().strftime(datetime_format)}"
        await update(self.wks, cell(*last_update_cell[::-1]), [[text]])

    async def get_all_entries(self) -> list[Entry]:
        rows = await get_all_values(self.wks)
        rows.pop(0)
        headers = await self.get_headers()
        entries = []
        for index, row in enumerate(rows, 2):
            row = [x.strip() for x in row][0:len(self.keys_headers)]
            row = sort_values_by_headers(headers, row, self.keys_headers)
            row.append(index)
            try:
                entries.append(Entry(*row))
            except ValueError:
                print(f"Error in row {index}: {row}")
                pass
        return entries

    async def get_not_returned_keys(self) -> list[Entry]:
        entries = await self.get_all_entries()
        not_returned_keys = []
        for entry in entries:
            if entry.time_returned is None:
                not_returned_keys.append(entry)
        return not_returned_keys

    async def set_return_time(self, entry: Entry, time_returned: datetime = None) -> None:
        headers = await self.get_headers()
        if time_returned is None:
            time_returned = datetime.now().strftime(datetime_format)
        index = headers.index(self.keys_headers["time_returned"])+1
        await update(
            self.wks,
            cell(index, entry.row),
            [[time_returned]]
        )

    async def set_return_time_by_key_name(self, key_name: str, time_returned: datetime = None) -> None:
        entries = await self.get_not_returned_keys()
        for entry in entries:
            if entry.key_name == key_name:
                await self.set_return_time(entry, time_returned)
                return


@dataclass
class Key:
    key_name: str
    count: int


class KeysTable:
    def __init__(self):
        with open(tables_path, "r", encoding="utf-8") as f:
            td = json.load(f)
        self.wks = spreadsheet.worksheet(td["keys_wks"])
        self.keys_headers = {
            "key_name": "Ключ",
            "count":    "Количество",
        }

    async def setup_table(self):
        await self.check_has_free_rows(1)
        await update(self.wks, cell(1, 1), [list(self.keys_headers.values())])
        await auto_resize(self.wks, 1, len(self.keys_headers)+1)

    async def get_headers(self):
        return (await row_values(self.wks, 1))[0:len(self.keys_headers)]

    async def check_has_free_rows(self, rows_count):
        current_rows = self.wks.row_count
        if current_rows < rows_count:
            await add_rows(self.wks, rows_count - current_rows)

    async def new_key(self, key_name, count):
        await self.add_key(Key(key_name, count))

    async def add_key(self, key_obj: Key):
        insert_row = len(await col_values(self.wks, 1)) + 1
        headers = await self.get_headers()
        values = []
        for header in headers:
            key = swap(self.keys_headers)[header]
            val = getattr(key_obj, key)
            values.append(val)
        await self.check_has_free_rows(insert_row)
        await update(self.wks, cell(1, insert_row), [values])
        await auto_resize(self.wks, 1, len(self.keys_headers)+1)

    async def get_all_keys(self) -> list[Key]:
        rows = await get_all_values(self.wks)
        rows.pop(0)
        headers = await self.get_headers()
        keys = []
        for row in rows:
            row = [x.strip() for x in row][0:len(self.keys_headers)]
            row = sort_values_by_headers(headers, row, self.keys_headers)
            try:
                keys.append(Key(*row))
            except ValueError:
                print(f"Error in table keys in row {row}")
                pass
        return keys


class Employee:
    def __init__(
            self,
            first_name: str,
            last_name: str,
            phone_number: str,
            telegram: str,
            roles: list[str]) -> None:
        self.first_name = first_name
        self.last_name = last_name
        self.phone_number = phone_number
        self.telegram = telegram

        if isinstance(roles, list):
            self.roles = roles
        elif isinstance(roles, str):
            self.roles = list(map(str.strip, roles.split(", ")))
        else:
            self.roles = []

    def __repr__(self):
        return (
            "----------\n"
            f"Name: {self.first_name} {self.last_name}\n"
            f"Phone number: {self.phone_number}\n"
            f"Telegram: {self.telegram}\n"
            f"Roles: {', '.join(self.roles)}"
            "\n----------\n"
        )


class EmployeesTable:
    def __init__(self):
        with open(tables_path, "r", encoding="utf-8") as f:
            td = json.load(f)
        self.wks = spreadsheet.worksheet(td["employees_wks"])
        self.keys_headers = {
            "first_name": "Имя",
            "last_name": "Фамилия",
            "phone_number": "Телефон",
            "telegram": "Телеграм",
            "roles": "Роли",
        }

    async def setup_table(self):
        await self.check_has_free_rows(1)
        await update(self.wks, cell(1, 1), [list(self.keys_headers.values())])
        await auto_resize(self.wks, 1, len(self.keys_headers)+1)

    async def get_headers(self):
        return (await row_values(self.wks, 1))[0:len(self.keys_headers)]

    async def check_has_free_rows(self, rows_count):
        current_rows = self.wks.row_count
        if current_rows < rows_count:
            await add_rows(self.wks, rows_count - current_rows)

    async def new_employee(
            self,
            first_name: str,
            last_name: str,
            phone: str,
            telegram: str,
            roles: list[str]
    ):
        await self.add_employee(Employee(first_name, last_name, phone, telegram, roles))

    async def add_employee(self, employee_obj: Employee):
        insert_row = len(await col_values(self.wks, 1)) + 1
        headers = await self.get_headers()
        values = []
        for header in headers:
            key = swap(self.keys_headers)[header]
            val = getattr(employee_obj, key)
            if isinstance(val, list):
                val = ", ".join(val)
            values.append(str(val))
        await self.check_has_free_rows(insert_row)
        await update(self.wks, cell(1, insert_row), [values])
        await auto_resize(self.wks, 1, len(self.keys_headers)+1)

    async def get_all_employees(self) -> list[Employee]:
        rows = await get_all_values(self.wks)
        rows.pop(0)
        headers = await self.get_headers()
        employees = []
        for row in rows:
            row = [x.strip() for x in row][0:len(self.keys_headers)]
            row = sort_values_by_headers(headers, row, self.keys_headers)
            try:
                employees.append(Employee(*row))
            except ValueError:
                print(f"Error in table employees in row {row}")
                pass
        return employees

    async def get_by_telegram(self, telegram: str):
        telegram = str(telegram)
        employees = await self.get_all_employees()
        for employee in employees:
            if employee.telegram == telegram:
                return employee

    async def get_security_employee(self):
        employees = await self.get_all_employees()
        for employee in employees:
            if "security" in employee.roles:
                return employee


# endregion


# region Tests


async def main():
    """Test code"""

    """kat = KeysAccountingTable()
    await clear(kat.wks)
    await kat.setup_table()
    await kat.new_entry("0001", "Савелий", "Алексеевич", "+7 (999) 999-99-99", "test!")
    await kat.new_entry("0002", "Савелий", "Алексеевич", "+7 (999) 999-99-99", "test2!")
    await kat.set_return_time((await kat.get_all_entries())[0])

    print(await kat.get_all_entries())
    print(await kat.get_not_returned_keys())"""

    """kt = KeysTable()
    await clear(kt.wks)
    await kt.setup_table()
    for i in range(10):
        await kt.new_key(f"000{i}", random.randint(1, 4))
    print(await kt.get_all_keys())"""

    et = EmployeesTable()
    # await clear(et.wks)
    # await et.setup_table()
    await et.new_employee("Савелий1", "Алексеевич1", "+7 (999) 999-99-99", "test!", ["admin"])
    print(await et.get_all_employees())

if __name__ == "__main__":
    asyncio.run(main())


# endregion

