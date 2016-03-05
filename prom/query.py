"""
Classes and stuff that handle querying the interface for a passed in Orm class
"""
import types
import datetime
import copy

from .utils import make_list, get_objects, make_dict, make_hash


class Iterator(object):
    """
    smartly iterate through a result set

    this is returned from the Query.get() and Query.all() methods, it acts as much
    like a list as possible to make using it as seemless as can be

    fields --
        has_more -- boolean -- True if there are more results in the db, false otherwise
        ifilter -- callback -- an iterator filter, all yielded rows will be passed
            through this callback and skipped if ifilter(row) returns True

    examples --
        # iterate through all the primary keys of some orm
        for pk in SomeOrm.query.all().pk:
            print pk

    http://docs.python.org/2/library/stdtypes.html#iterator-types
    """
    def __init__(self, results, orm=None, has_more=False, query=None):
        """
        create a result set iterator

        results -- list -- the list of results
        orm -- Orm -- the Orm class that each row in results should be wrapped with
        has_more -- boolean -- True if there are more results
        query -- Query -- the query instance that produced this iterator
        """
        self.results = results
        self.ifilter = None # https://docs.python.org/2/library/itertools.html#itertools.ifilter
        self.orm = orm
        self.has_more = has_more
        self.query = query.copy()
        self._values = False
        self.reset()

    def reset(self):
        #self.iresults = (self._get_result(d) for d in self.results)
        #self.iresults = self.create_generator()
        inormalize = (self._get_result(d) for d in self.results)
        self.iresults = (o for o in inormalize if self._filtered(o))

    def next(self):
        return self.iresults.next()

    def values(self):
        """
        similar to the dict.values() method, this will only return the selected fields
        in a tuple

        return -- self -- each iteration will return just the field values in
            the order they were selected, if you only selected one field, than just that field
            will be returned, if you selected multiple fields than a tuple of the fields in
            the order you selected them will be returned
        """
        self._values = True
        self.field_names = self.query.fields_select
        self.fcount = len(self.field_names)
        if not self.fcount:
            raise ValueError("no select fields were set, so cannot iterate values")

        return self

    def __iter__(self):
        self.reset()
        return self

    def __nonzero__(self):
        return True if self.count() else False

    def __len__(self):
        return self.count()

    def count(self):
        """list interface compatibility"""
        return len(self.results)

    def __getitem__(self, k):
        k = int(k)
        return self._get_result(self.results[k])

    def pop(self, k=-1):
        """list interface compatibility"""
        k = int(k)
        return self._get_result(self.results.pop(k))

    def reverse(self):
        """list interface compatibility"""
        self.results.reverse()
        self.reset()

    def __reversed__(self):
        self.reverse()
        return self

    def sort(self, *args, **kwargs):
        """list interface compatibility"""
        self.results.sort(*args, **kwargs)
        self.reset()

    def __getattr__(self, k):
        """
        this allows you to focus in on certain fields of results

        It's just an easier way of doing: (getattr(x, k, None) for x in self)
        """
        field_name = self.orm.schema.field_name(k)
        return (getattr(r, field_name, None) for r in self)

    def create_generator(self):
        """put all the pieces together to build a generator of the results"""
        inormalize = (self._get_result(d) for d in self.results)
        return (o for o in inormalize if not self._filtered(o))

    def _get_result(self, d):
        r = None
        if self._values:
            field_vals = [d.get(fn, None) for fn in self.field_names]
            r = field_vals if self.fcount > 1 else field_vals[0]

        else:
            if self.orm:
                r = self.orm.populate(d)
            else:
                r = d

        return r

    def _filtered(self, o):
        """run orm o through the filter, if True then orm o should be included"""
        return self.ifilter(o) if self.ifilter else True


class AllIterator(Iterator):
    """
    Similar to Iterator, but will chunk up results and make another query for the next
    chunk of results until there are no more results of the passed in Query(), so you
    can just iterate through every row of the db without worrying about pulling too
    many rows at one time

    NOTE -- pop() may have unexpected results
    """
    def __init__(self, query):
        limit, offset, _ = query.get_bounds()
        if not limit:
            limit = 5000

        self.chunk_limit = limit
        self.offset = offset
        super(AllIterator, self).__init__(results=[], orm=query.orm, query=query)

    def __getitem__(self, k):
        v = None
        k = int(k)
        lower_bound = self.offset
        upper_bound = lower_bound + self.chunk_limit
        if k >= lower_bound and k < upper_bound:
            # k should be in this result set
            i = k - lower_bound
            v = self.results[i]

        else:
            # k is not in here, so let's just grab it
            q = self.query.copy()
            orm = q.set_offset(k).get_one()
            if orm:
                v = self._get_result(orm.fields)
            else:
                raise IndexError("results index out of range")

        return v

    def count(self):
        ret = 0
        if self.results.has_more:
            # we need to do a count query
            q = self.query.copy()
            q.set_limit(0).set_offset(0)
            ret = q.count()
        else:
            ret = (self.offset - self.start_offset) + len(self.results)

        return ret

    def __iter__(self):
        has_more = True
        self.reset()
        while has_more:
            has_more = self.results.has_more
            for r in self.results:
                yield r

            self.offset += self.chunk_limit
            self._set_results()

    def _set_results(self):
        self.results = self.query.set_offset(self.offset).get(self.chunk_limit)
        if self._values:
            self.results = self.results.values()

    def reset(self):
        set_results = False
        if hasattr(self, 'start_offset'):
            set_results = self.offset != self.start_offset
        else:
            self.start_offset = self.offset
            set_results = True

        if set_results:
            self.offset = self.start_offset
            self._set_results()

        else:
            self.results.reset()

    def values(self):
        self.results = self.results.values()
        return super(AllIterator, self).values()


class Query(object):
    """
    Handle standard query creation and allow interface querying

    Query instances are tied to an orm.Model class, so you have to pass orm_class
    in

    example --
        q = Query(orm)
        q.is_foo(1).desc_bar().set_limit(10).set_page(2).get()
    """
    @property
    def interface(self):
        return self.orm_class.interface

    @property
    def fields(self):
        return dict(self.fields_set)

    @property
    def fields_select(self):
        return [select_field for select_field, _ in self.fields_set]

    def __init__(self, orm_class, *args, **kwargs):
        """initialize a query instance

        orm_class -- orm.Model -- a class this query will use as a factory
        """
        self.orm_class = orm_class
        self.interface_query = self.interface.create_query()

        self.args = args
        self.kwargs = kwargs
        # the idea here is to set this to False if there is a condition that will
        # automatically cause the query to fail but not necessarily be an error, 
        # the best example is the IN (...) queries, if you do self.in_foo([]).get()
        # that will fail because the list was empty, but a value error shouldn't
        # be raised because a common case is: self.if_foo(Bar.query.is_che(True).pks).get()
        # which should result in an empty set if there are no rows where che = TRUE
        self.can_get = True

    def ref(self, orm_classpath, cls_pk=None):
        """
        takes a classpath to allow query-ing from another Orm class

        the reason why it takes string paths is to avoid infinite recursion import 
        problems because an orm class from module A might have a ref from module B
        and sometimes it is handy to have module B be able to get the objects from
        module A that correspond to the object in module B, but you can't import
        module A into module B because module B already imports module A.

        orm_classpath -- string -- a full python class path (eg, foo.bar.Che)
        cls_pk -- mixed -- automatically set the where field of orm_classpath 
            that references self.orm to the value in cls_pk if present
        return -- Query()
        """
        # split orm from module path
        orm_module, orm_class = get_objects(orm_classpath)
        q = orm_class.query
        if cls_pk:
            for fn, f in orm_class.schema.fields.items():
                cls_ref_s = f.schema
                if cls_ref_s and self.orm.schema == cls_ref_s:
                    q.is_field(fn, cls_pk)
                    break

        return q

    def __iter__(self):
        #return self.all()
        #return self.get()
        # NOTE -- for some reason I need to call AllIterator.__iter__() explicitely
        # because it would call AllIterator.next() even though AllIterator.__iter__
        # returns a generator, not sure what's up
        return self.get() if self.bounds else self.all().__iter__()

    def copy(self):
        """nice handy wrapper around the deepcopy"""
        return copy.deepcopy(self)

    def __deepcopy__(self, memodict={}):
        q = type(self)(self.orm)
        for key, val in self.__dict__.iteritems():
            if isinstance(val, types.ListType):
                setattr(q, key, list(val))

            elif isinstance(val, types.DictType):
                setattr(q, key, dict(val))

            elif isinstance(val, types.TupleType):
                setattr(q, key, tuple(val))

            else:
                setattr(q, key, val)

        return q

    def select_field(self, field_name):
        """set a field to be selected"""
        return self.set_field(field_name, None)

    def select_fields(self, *fields):
        """set multiple fields to be selected"""
        if fields:
            if not isinstance(fields[0], types.StringTypes): 
                fields = list(fields[0]) + list(fields)[1:]

        for field_name in fields:
            self.select_field(field_name)

    def set_field(self, field_name, field_val=None):
        """
        set a field into .fields attribute

        n insert/update queries, these are the fields that will be inserted/updated into the db
        """
        self.fields_set.append([field_name, field_val])
        return self

    def set_fields(self, fields=None, *fields_args, **fields_kwargs):
        """
        completely replaces the current .fields with fields and fields_kwargs combined
        """
        if fields_args:
            fields = [fields]
            fields.extend(fields_args)
            for field_name in fields:
                self.set_field(field_name)

        elif fields_kwargs:
            fields = make_dict(fields, fields_kwargs)
            for field_name, field_val in fields.items():
                self.set_field(field_name, field_val)

        else:
            if isinstance(fields, (types.DictType, types.DictProxyType)):
                for field_name, field_val in fields.items():
                    self.set_field(field_name, field_val)

            else:
                for field_name in fields:
                    self.set_field(field_name)

        return self

    def is_field(self, field_name, *field_val, **field_kwargs):
        fv = field_val[0] if field_val else None
        self.fields_where.append(["is", field_name, fv, field_kwargs])
        return self

    def not_field(self, field_name, *field_val, **field_kwargs):
        fv = field_val[0] if field_val else None
        self.fields_where.append(["not", field_name, fv, field_kwargs])
        return self

    def between_field(self, field_name, low, high):
        self.lte_field(field_name, low)
        self.gte_field(field_name, high)
        return self

    def lte_field(self, field_name, *field_val, **field_kwargs):
        fv = field_val[0] if field_val else None
        self.fields_where.append(["lte", field_name, fv, field_kwargs])
        return self

    def lt_field(self, field_name, *field_val, **field_kwargs):
        fv = field_val[0] if field_val else None
        self.fields_where.append(["lt", field_name, fv, field_kwargs])
        return self

    def gte_field(self, field_name, *field_val, **field_kwargs):
        fv = field_val[0] if field_val else None
        self.fields_where.append(["gte", field_name, fv, field_kwargs])
        return self

    def gt_field(self, field_name, *field_val, **field_kwargs):
        fv = field_val[0] if field_val else None
        self.fields_where.append(["gt", field_name, fv, field_kwargs])
        return self

    def in_field(self, field_name, *field_vals, **field_kwargs):
        """
        field_vals -- list -- a list of field_val values
        """
        fv = make_list(field_vals[0]) if field_vals else None
        if field_kwargs:
            for k in field_kwargs:
                if not field_kwargs[k]:
                    raise ValueError("Cannot IN an empty list")

                field_kwargs[k] = make_list(field_kwargs[k])

        else:
            if not fv: self.can_get = False

        self.fields_where.append(["in", field_name, fv, field_kwargs])
        return self

    def nin_field(self, field_name, *field_vals, **field_kwargs):
        """
        field_vals -- list -- a list of field_val values
        """
        fv = make_list(field_vals[0]) if field_vals else None
        if field_kwargs:
            for k in field_kwargs:
                if not field_kwargs[k]:
                    raise ValueError("Cannot IN an empty list")

                field_kwargs[k] = make_list(field_kwargs[k])

        else:
            if not fv: self.can_get = False

        self.fields_where.append(["nin", field_name, fv, field_kwargs])
        return self

    def sort_field(self, field_name, direction, field_vals=None):
        """
        sort this query by field_name in directrion

        field_name -- string -- the field to sort on
        direction -- integer -- negative for DESC, positive for ASC
        field_vals -- list -- the order the rows should be returned in
        """
        if direction > 0:
            direction = 1
        elif direction < 0:
            direction = -1
        else:
            raise ValueError("direction {} is undefined".format(direction))

        self.fields_sort.append([direction, field_name, list(field_vals) if field_vals else field_vals])
        return self

    def asc_field(self, field_name, field_vals=None):
        self.sort_field(field_name, 1, field_vals)
        return self

    def desc_field(self, field_name, field_vals=None):
        self.sort_field(field_name, -1, field_vals)
        return self

    def __getattr__(self, method_name):

        command, field_name = self._split_method(method_name)

        def callback(*args, **kwargs):
            field_method_name = "{}_field".format(command)
            command_field_method = None

            if getattr(type(self), field_method_name, None):
                command_field_method = getattr(self, field_method_name)
            else:
                raise AttributeError('No "{}" method derived from "{}"'.format(field_method_name, method_name))

            return command_field_method(field_name, *args, **kwargs)

        return callback

    def _split_method(self, method_name):
        try:
            command, field_name = method_name.split(u"_", 1)
        except ValueError:
            raise AttributeError("invalid command_method: {}".format(method_name))

        if self.orm: field_name = self.orm.schema.field_name(field_name)

        return command, field_name

    def set_limit(self, limit):
        self.bounds.limit = limit
        return self

    def set_offset(self, offset):
        self.bounds.offset = offset
        return self

    def set_page(self, page):
        self.bounds.page = page
        return self

    def get(self, limit=None, page=None):
        """
        get results from the db

        return -- Iterator()
        """
        if limit is not None:
            self.set_limit(limit)
        if page is not None:
            self.set_page(page)

        has_more = False
        limit, offset, limit_paginate = self.get_bounds()
        if limit_paginate:
            self.set_limit(limit_paginate)

        self.default_val = []
        results = self._query('get')

        if limit_paginate:
            self.set_limit(limit)
            if len(results) == limit_paginate:
                has_more = True
                results.pop(limit)

        iterator_class = self.orm.iterator_class if self.orm else Iterator
        return iterator_class(results, orm=self.orm, has_more=has_more, query=self)

    def all(self):
        """
        return every possible result for this query

        This is smart about returning results and will use the set limit (or a default if no
        limit was set) to chunk up the results, this means you can work your way through
        really big result sets without running out of memory

        return -- Iterator()
        """
        return AllIterator(self)

    def get_one(self):
        """get one row from the db"""
        self.default_val = None
        o = self.default_val
        d = self._query('get_one')
        if d:
            o = self.orm.populate(d)
        return o

    def values(self, limit=None, page=None):
        """
        convenience method to get just the values from the query (same as get().values())

        if you want to get all values, you can use: self.all().values()
        """
        return self.get(limit=limit, page=page).values()

    def value(self):
        """convenience method to just get one value or tuple of values for the query"""
        field_vals = None
        field_names = self.fields_select
        fcount = len(field_names)
        if fcount:
            d = self._query('get_one')
            if d:
                field_vals = [d.get(fn, None) for fn in field_names]
                if fcount == 1:
                    field_vals = field_vals[0]

        else:
            raise ValueError("no select fields were set, so cannot return value")

        return field_vals

    def pks(self, limit=None, page=None):
        """convenience method for setting select_pk().values() since this is so common"""
        self.fields_set = []
        return self.select_pk().values(limit, page)

    def pk(self):
        """convenience method for setting select_pk().value() since this is so common"""
        self.fields_set = []
        return self.select_pk().value()

    def get_pks(self, field_vals):
        """convenience method for running in__id([...]).get() since this is so common"""
        field_name = self.orm.schema.pk.name
        return self.in_field(field_name, field_vals).get()

    def get_pk(self, field_val):
        """convenience method for running is_pk(_id).get_one() since this is so common"""
        field_name = self.orm.schema.pk.name
        return self.is_field(field_name, field_val).get_one()

    def first(self):
        """convenience method for running asc__id().get_one()"""
        return self.asc__id().get_one()

    def last(self):
        """convenience method for running desc__id().get_one()"""
        return self.desc__id().get_one()

    def count(self):
        """return the count of the criteria"""

        # count queries shouldn't care about sorting
        fields_sort = self.fields_sort
        self.fields_sort = []

        self.default_val = 0
        ret = self._query('count')

        # restore previous values now that count is done
        self.fields_sort = fields_sort

        return ret

    def has(self):
        """returns true if there is atleast one row in the db matching the query, False otherwise"""
        v = self.get_one()
        return True if v else False

    def insert(self):
        """persist the .fields"""
        self.default_val = 0
        fields = self.orm.depart(self.fields, is_update=False)
        self.set_fields(fields)
        return self.orm.interface.insert(
            self.orm.schema,
            fields
        )

        return self.orm.interface.insert(self.orm.schema, self.fields)

    def update(self):
        """persist the .fields using .fields_where"""
        self.default_val = 0
        fields = self.orm.depart(self.fields, is_update=True)
        self.set_fields(fields)
        return self.orm.interface.update(
            self.orm.schema,
            fields,
            self
        )
        #return self._query('update')

    def delete(self):
        """remove fields matching the where criteria"""
        self.default_val = None
        return self._query('delete')

    def raw(self, query_str, *query_args, **query_options):
        """
        use the interface.query() method to pass in your own raw query without
        any processing

        NOTE -- This will allow you to make any raw query and will usually return
        raw results, it won't wrap those results in a prom.Orm iterator instance
        like other methods like .all() and .get()

        query_str -- string -- the raw query for whatever the backend interface is
        query_args -- list -- if you have named parameters, you can pass in the values
        **query_options -- dict -- key:val options for the backend, these are very backend specific
        return -- mixed -- depends on the backend and the type of query
        """
        i = self.orm.interface
        return i.query(query_str, *query_args, **query_options)

    def _query(self, method_name):
        if not self.can_get: return self.default_val
        i = self.orm.interface
        s = self.orm.schema
        return getattr(i, method_name)(s, self) # i.method_name(schema, query)


class CacheQuery(Query):
    """a standard query caching skeleton class with the idea that it would be expanded
    upon on a per project or per model basis"""

    cache_ttl = 3600
    """how long you should cache results for cacheable queries"""

    def cache_invalidate(self, method_name):
        cached = getattr(self, "cached", {})
        cached.clear()

    def cache_key(self, method_name):
        """decides if this query is cacheable, returns a key if it is, otherwise empty"""
        key = make_hash(method_name, self.fields_set, self.fields_where, self.fields_sort)
        return key

    def cache_set(self, key, result):
        cached = getattr(self, "cached", {})
        now = datetime.datetime.utcnow()
        cached[key] = {
            "datetime": now,
            "result": result
        }

    def cache_get(self, key):
        result = None
        cache_hit = False
        cached = getattr(self, "cached", {})
        now = datetime.datetime.utcnow()
        if key in cached:
            td = now - cached[key]["datetime"]
            if td.total_seconds() < self.cached_ttl:
                cache_hit = True
                result = cached[key]["result"]

        return result, cache_hit

    def _query(self, method_name):
        cache_hit = False
        cache_key = self.cache_key(method_name)
        if cache_key:
            result, cache_hit = self.cache_get(cache_key)

        if not cache_hit:
            result = super(CacheQuery, self)._query(method_name)
            if cache_key:
                self.cache_set(cache_key, result)

        self.cache_hit = cache_hit
        return result

    def update(self):
        ret = super(CachedQuery, self).update()
        if ret:
            self.invalidate("update")
        return ret

    def delete(self):
        ret = super(CachedQuery, self).delete()
        if ret:
            self.invalidate("delete")
        return ret

