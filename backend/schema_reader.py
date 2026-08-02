from database_manager import db_manager


class SchemaReader:

    def __init__(self):
        self.connection = None
    def _get_connection(self):
        self.connection = db_manager.get_connection()
        return self.connection

    def get_tables(self):
        connection = self._get_connection()
        cursor = connection.cursor()

        cursor.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            AND name NOT LIKE 'sqlite_%';
        """)

        return [table[0] for table in cursor.fetchall()]

    def get_columns(self, table_name):
        connection = self._get_connection()
        cursor = connection.cursor()

        cursor.execute(f"PRAGMA table_info('{table_name}')")

        columns = []

        for column in cursor.fetchall():

            columns.append({
                "column_name": column[1],
                "data_type": column[2],
                "not_null": bool(column[3]),
                "default_value": column[4],
                "primary_key": bool(column[5])
            })

        return columns

    def get_foreign_keys(self, table_name):

        connection = self._get_connection()
        cursor = connection.cursor()

        cursor.execute(f"PRAGMA foreign_key_list('{table_name}')")

        foreign_keys = []

        for fk in cursor.fetchall():

            foreign_keys.append({
                "column": fk[3],
                "references_table": fk[2],
                "references_column": fk[4]
            })

        return foreign_keys

    def get_indexes(self, table_name):
        connection = self._get_connection()
        cursor = connection.cursor()
        escaped = table_name.replace("'", "''")
        cursor.execute(f"PRAGMA index_list('{escaped}')")
        indexes = []
        for index_row in cursor.fetchall():
            index_name = index_row[1]
            unique = bool(index_row[2])
            escaped_index = index_name.replace("'", "''")
            cursor.execute(f"PRAGMA index_info('{escaped_index}')")
            columns = [row[2] for row in cursor.fetchall()]
            if columns:
                indexes.append({
                    "name": index_name,
                    "columns": columns,
                    "unique": unique,
                })
        return indexes

    def read_schema(self):

        schema = {}

        tables = self.get_tables()

        for table in tables:

            indexes = self.get_indexes(table)
            schema[table] = {

                "columns": self.get_columns(table),

                "foreign_keys": self.get_foreign_keys(table),

                "indexes": indexes,

                "constraints": [
                    {"name": index["name"], "type": "unique", "columns": index["columns"]}
                    for index in indexes
                    if index["unique"]
                ],

            }

        return schema
    

    def get_schema_prompt(self):

        schema = self.read_schema()

        prompt = "DATABASE SCHEMA\n"
        prompt += "=" * 60 + "\n\n"

        for table_name, table_info in schema.items():

            prompt += f"TABLE: {table_name}\n"
            prompt += "-" * 40 + "\n"

            prompt += "Columns:\n"

            for column in table_info["columns"]:

                line = f"• {column['column_name']} ({column['data_type']})"

                if column["primary_key"]:
                    line += " [PRIMARY KEY]"

                if column["not_null"]:
                    line += " [NOT NULL]"

                prompt += line + "\n"

            if table_info["foreign_keys"]:

                prompt += "\nForeign Keys:\n"

                for fk in table_info["foreign_keys"]:

                    prompt += (
                        f"• {fk['column']} "
                        f"→ {fk['references_table']}."
                        f"{fk['references_column']}\n"
                )

                prompt += "\n"

            prompt += "\n"

        return prompt
    
    def get_database_summary(self):

        schema = self.read_schema()

        total_tables = len(schema)

        total_columns = 0

        for table in schema.values():
            total_columns += len(table["columns"])

        return {
        "tables": total_tables,
        "columns": total_columns
        }
    
    def get_table_count(self):

        return len(self.get_tables())
    
    def table_exists(self, table_name):

        return table_name.lower() in [
            table.lower()
            for table in self.get_tables()
    ]

    def get_column_names(self):

        schema = self.read_schema()

        columns = {}

        for table, info in schema.items():

            columns[table] = []

            for col in info["columns"]:
                 columns[table].append(col["column_name"])

        return columns

schema_reader = SchemaReader()
