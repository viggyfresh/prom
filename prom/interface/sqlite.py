"""
Bindings for SQLite

https://docs.python.org/2/library/sqlite3.html
https://docs.python.org/2/library/sqlite3.html
"""
import os
import types
import decimal
import datetime

# third party
import sqlite3

# first party
from .base import SQLInterface


class SQLiteRowDict(sqlite3.Row):
    def get(self, k, default_val=None):
        r = default_val
        r = self[str(k)]
        return r


class SQLiteConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super(SQLiteConnection, self).__init__(*args, **kwargs)
        self.closed = 0

    def close(self, *args, **kwargs):
        r = super(SQLiteConnection, self).close(*args, **kwargs)
        self.closed = 1
        return r


class BooleanType(object):
    @staticmethod
    def adapt(val):
        return int(str(val))

    @staticmethod
    def convert(val):
        return bool(str(val))


class NumericType(object):
    @staticmethod
    def adapt(val):
        return float(str(val))

    @staticmethod
    def convert(val):
        return decimal.Decimal(str(val))


class StringType(object):
    """this just makes sure 8-bit bytestrings get converted ok"""
    @staticmethod
    def adapt(val):
        if isinstance(val, str):
            val = val.decode('utf-8')

        return val


class Interface(SQLInterface):

    val_placeholder = '?'

    def _connect(self, connection_config):
        path = ''
        dsn = getattr(connection_config, 'dsn', '')
        if dsn:
            host = connection_config.host
            db = connection_config.database
            if not host:
                path = os.sep + db

            elif not db:
                path = host

            else:
                path = os.sep.join([host, db])

        else:
            path = connection_config.database

        if not path:
            raise ValueError("no sqlite db path found in connection_config")

        # https://docs.python.org/2/library/sqlite3.html#default-adapters-and-converters
        options = {
            'isolation_level': None,
            'detect_types': sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES,
            'factory': SQLiteConnection
        }
        for k in ['timeout', 'detect_types', 'isolation_level', 'check_same_thread', 'factory', 'cached_statements']:
            if k in connection_config.options:
                options[k] = connection_config.options[k]

        self.connection = sqlite3.connect(path, **options)
        # https://docs.python.org/2/library/sqlite3.html#row-objects
        self.connection.row_factory = SQLiteRowDict
        # https://docs.python.org/2/library/sqlite3.html#sqlite3.Connection.text_factory
        self.connection.text_factory = StringType.adapt

        sqlite3.register_adapter(decimal.Decimal, NumericType.adapt)
        sqlite3.register_converter('NUMERIC', NumericType.convert)

        sqlite3.register_adapter(bool, BooleanType.adapt)
        sqlite3.register_converter('BOOLEAN', BooleanType.convert)

        # turn on foreign keys
        # http://www.sqlite.org/foreignkeys.html
        self._query('PRAGMA foreign_keys = ON', ignore_result=True);

    def _get_tables(self, table_name):
        query_str = 'SELECT tbl_name FROM sqlite_master WHERE type = ?'
        query_args = ['table']

        if table_name:
            query_str += ' AND name = ?'
            query_args.append(str(table_name))

        ret = self._query(query_str, query_args)
        return [r['tbl_name'] for r in ret]

    def get_field_SQL(self, field_name, field_options):
        """
        returns the SQL for a given field with full type information

        field_name -- string -- the field's name
        field_options -- dict -- the set options for the field

        return -- string -- the field type (eg, foo BOOL NOT NULL)
        """
        field_type = ""

        if field_options.get('pk', False):
            field_type = 'INTEGER PRIMARY KEY'

        else:
            if issubclass(field_options['type'], bool):
                field_type = 'BOOLEAN'

            elif issubclass(field_options['type'], int):
                field_type = 'INTEGER'

            elif issubclass(field_options['type'], long):
                field_type = 'BIGINT'

            elif issubclass(field_options['type'], types.StringTypes):
                if 'size' in field_options:
                    field_type = 'CHARACTER({})'.format(field_options['size'])
                elif 'max_size' in field_options:
                    field_type = 'VARCHAR({})'.format(field_options['max_size'])
                else:
                    field_type = 'TEXT'

                if field_options.get('ignore_case', False):
                    field_type += ' COLLATE NOCASE'

            elif issubclass(field_options['type'], datetime.datetime):
                #field_type = 'DATETIME'
                field_type = 'TIMESTAMP'

            elif issubclass(field_options['type'], datetime.date):
                field_type = 'DATE'

            elif issubclass(field_options['type'], float):
                field_type = 'REAL'
                size = field_options.get('size', field_options.get('max_size', 0))
                if size > 6:
                    field_type = 'DOUBLE PRECISION'

            elif issubclass(field_options['type'], decimal.Decimal):
                field_type = 'NUMERIC'

            else:
                raise ValueError('unknown python type: {}'.format(field_options['type'].__name__))

            if field_options.get('required', False):
                field_type += ' NOT NULL'
            else:
                field_type += ' NULL'

            if 'ref' in field_options: # strong ref, it deletes on fk row removal
                ref_s = field_options['ref']
                field_type += ' REFERENCES {} ({}) ON UPDATE CASCADE ON DELETE CASCADE'.format(ref_s.table, ref_s.pk)

            elif 'weak_ref' in field_options: # weak ref, it sets column to null on fk row removal
                ref_s = field_options['weak_ref']
                field_type += ' REFERENCES {} ({}) ON UPDATE CASCADE ON DELETE SET NULL'.format(ref_s.table, ref_s.pk)

        return '{} {}'.format(field_name, field_type)

    def _set_table(self, schema):
        query_str = []
        query_str.append("CREATE TABLE {} (".format(schema.table))

        query_fields = []
        for field_name, field_options in schema.fields.iteritems():
            query_fields.append('  {}'.format(self.get_field_SQL(field_name, field_options)))

        query_str.append(",{}".format(os.linesep).join(query_fields))
        query_str.append(')')
        query_str = os.linesep.join(query_str)
        ret = self._query(query_str, ignore_result=True)

    def _set_index(self, schema, name, fields, **index_options):
        """
        https://www.sqlite.org/lang_createindex.html
        """
        query_str = 'CREATE {}INDEX IF NOT EXISTS {}_{} ON {} ({})'.format(
            'UNIQUE ' if index_options.get('unique', False) else '',
            schema,
            name,
            schema,
            ', '.join(fields)
        )

        return self._query(query_str, ignore_result=True)

    def _get_indexes(self, schema):
        """return all the indexes for the given schema"""
        # http://www.sqlite.org/pragma.html#schema
        # http://www.mail-archive.com/sqlite-users@sqlite.org/msg22055.html
        # http://stackoverflow.com/questions/604939/
        ret = {}
        rs = self._query('PRAGMA index_list({})'.format(schema))
        if rs:
            for r in rs:
                iname = r['name']
                ret.setdefault(iname, [])
                indexes = self._query('PRAGMA index_info({})'.format(r['name']))
                for idict in indexes:
                    ret[iname].append(idict['name'])

        return ret

    def _insert(self, schema, d):

        # get the primary key
        field_formats = []
        field_names = []
        query_vals = []
        for field_name, field_val in d.iteritems():
            field_names.append(field_name)
            field_formats.append(self.val_placeholder)
            query_vals.append(field_val)

        query_str = 'INSERT INTO {} ({}) VALUES ({})'.format(
            schema,
            ', '.join(field_names),
            ', '.join(field_formats)
        )

        ret = self._query(query_str, query_vals, cursor_result=True)
        return ret.lastrowid

    def _delete_table(self, schema):
        query_str = 'DROP TABLE IF EXISTS {}'.format(str(schema))
        ret = self._query(query_str, ignore_result=True)

    def _handle_error(self, schema, e):
        ret = False
        if isinstance(e, sqlite3.OperationalError):
            e_msg = str(e)
            if schema.table in e_msg:
                if "no such table" in e_msg:
                    ret = self._set_all_tables(schema)

                elif "column" in e_msg:
                    # "table yscrmiklbgdtx has no column named che"
                    try:
                        ret = self._set_all_fields(schema)
                    except ValueError, e:
                        ret = False

        return ret

    def _get_fields(self, schema):
        """return all the fields for the given schema"""
        ret = []
        query_str = 'PRAGMA table_info({})'.format(schema)
        fields = self._query(query_str)
        return set((d['name'] for d in fields))

    def _normalize_date_SQL(self, field_name, field_kwargs):
        """
        allow extracting information from date

        http://www.sqlite.org/lang_datefunc.html
        """
        fstrs = []
        k_opts = {
            'day': "CAST(strftime('%d', {}) AS integer)",
            'hour': "CAST(strftime('%H', {}) AS integer)",
            'doy': "CAST(strftime('%j', {}) AS integer)", # day of year
            'julian_day': "strftime('%J', {})", # YYYY-MM-DD
            'month': "CAST(strftime('%m', {}) AS integer)",
            'minute': "CAST(strftime('%M', {}) AS integer)",
            'dow': "CAST(strftime('%w', {}) AS integer)", # day of week 0 = sunday
            'week': "CAST(strftime('%W', {}) AS integer)",
            'year': "CAST(strftime('%Y', {}) AS integer)"
        }

        for k, v in field_kwargs.iteritems():
            fstrs.append([k_opts[k].format(field_name), self.val_placeholder, v])

        return fstrs

    def _normalize_sort_SQL(self, field_name, field_vals, sort_dir_str):
        # this solution is based off: http://postgresql.1045698.n5.nabble.com/ORDER-BY-FIELD-feature-td1901324.html
        # see also: https://gist.github.com/cpjolicoeur/3590737
        fvi = None
        if sort_dir_str == 'ASC':
            fvi = (t for t in enumerate(field_vals)) 

        else:
            fvi = (t for t in enumerate(reversed(field_vals))) 

        query_sort_str = ['  CASE {}'.format(field_name)]
        query_args = []
        for i, v in fvi:
            query_sort_str.append('    WHEN {} THEN {}'.format(self.val_placeholder, i))
            query_args.append(v)

        query_sort_str.append('  END'.format(field_name))
        query_sort_str = "\n".join(query_sort_str)
        return query_sort_str, query_args


