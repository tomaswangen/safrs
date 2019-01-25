# -*- coding: utf-8 -*-
# pylint: disable=redefined-builtin,invalid-name, line-too-long, protected-access
#
# This code implements REST HTTP methods and sqlalchemy to json serialization
#
# Configuration parameters:
# - endpoint
#
# todo:
# - expose canonical endpoints
# - move all swagger related stuffto swagger_doc
# - fieldsets
#
'''
http://jsonapi.org/format/#content-negotiation-servers

Server Responsibilities
Servers MUST send all JSON API data in response documents with the header
"Content-Type: application/vnd.api+json" without any media type parameters.

Servers MUST respond with a 415 Unsupported Media Type status code if a request specifies the header
"Content-Type: application/vnd.api+json" with any media type parameters.
This should be implemented by the app, for example using @app.before_request  and @app.after_request
'''

import traceback
import logging
import re
import json
import werkzeug
import sqlalchemy
import sqlalchemy.orm.dynamic
import sqlalchemy.orm.collections
from sqlalchemy.orm.interfaces import MANYTOONE
from functools import wraps
from flask import make_response, url_for
from flask import jsonify, request
from flask_restful.utils import cors
from flask_restful_swagger_2 import Resource
from flask_restful import abort

import safrs
from .db import SAFRSBase
from .swagger_doc import is_public, default_paging_parameters
from .swagger_doc import parse_object_doc, get_http_methods
from .errors import ValidationError, GenericError, NotFoundError
from .config import get_config
from .json_encoder import SAFRSFormattedResponse

INCLUDE_ALL = '+all'

def http_method_decorator(fun):
    '''
        Decorator for the REST methods
        - commit the database
        - convert all exceptions to a JSON serializable GenericError

        This method will be called for all requests
    '''

    @wraps(fun)
    def method_wrapper(*args, **kwargs):
        try:
            result = fun(*args, **kwargs)
            safrs.DB.session.commit()
            return result

        except (ValidationError, GenericError, NotFoundError) as exc:
            traceback.print_exc()
            status_code = getattr(exc, 'status_code')
            message = exc.message

        except werkzeug.exceptions.NotFound:
            status_code = 404
            message = 'Not Found'

        except Exception as exc:
            status_code = getattr(exc, 'status_code', 500)
            traceback.print_exc()
            if safrs.LOGGER.getEffectiveLevel() > logging.DEBUG:
                message = 'Unknown Error'
            else:
                message = str(exc)

        safrs.DB.session.rollback()
        errors = dict(detail=message)
        abort(status_code, errors=[errors])

    return method_wrapper


def api_decorator(cls, swagger_decorator):
    '''
        Decorator for the API views:
            - add swagger documentation ( swagger_decorator )
            - add cors
            - add generic exception handling

        We couldn't use inheritance because the rest method decorator
        references the cls.SAFRSObject which isn't known
    '''

    cors_domain = get_config('cors_domain')
    cls.http_methods = {} # holds overridden http methods, note: cls also has the "methods" set, but it's not related to this
    for method_name in ['get', 'post', 'delete', 'patch', 'put', 'options']: # HTTP methods
        method = getattr(cls, method_name, None)
        if not method:
            continue

        decorated_method = method

        # if the SAFRSObject has a custom http method decorator, use it
        # e.g. SAFRSObject.get
        custom_method = getattr(cls.SAFRSObject, method_name, None)
        if custom_method:
            decorated_method = custom_method
            # keep the default method as parent_<method_name>, e.g. parent_get
            parent_method = getattr(cls, method_name)
            cls.http_methods[method_name] = lambda *args, **kwargs: parent_method(*args, **kwargs)

        # Apply custom decorators, specified as class variable list
        try:
            # Add swagger documentation
            decorated_method = swagger_decorator(decorated_method)
        except RecursionError:
            # Got this error when exposing WP DB, TODO: investigate where it comes from
            safrs.LOGGER.error('Failed to generate documentation for {} {} (Recursion Error)'.format(cls, decorated_method))
        except Exception as exc:
            safrs.LOGGER.error(exc)
            traceback.print_exc()
            safrs.LOGGER.error('Failed to generate documentation for {}'.format(decorated_method))

        # Add cors
        if cors_domain is not None:
            decorated_method = cors.crossdomain(origin=cors_domain)(decorated_method)
        # Add exception handling
        decorated_method = http_method_decorator(decorated_method)

        setattr(decorated_method, 'SAFRSObject', cls.SAFRSObject)
        for custom_decorator in getattr(cls.SAFRSObject, 'custom_decorators', []):
            decorated_method = custom_decorator(decorated_method)


        setattr(cls, method_name, decorated_method)

    return cls

def paginate(object_query, SAFRSObject=None):
    '''
        - returns
            links, instances, count

        http://jsonapi.org/format/#fetching-pagination

        A server MAY choose to limit the number of resources returned
         in a response to a subset (“page”) of the whole set available.
        A server MAY provide links to traverse a paginated data set (“pagination links”).
        Pagination links MUST appear in the links object that corresponds
         to a collection. To paginate the primary data, supply pagination links
         in the top-level links object. To paginate an included collection
         returned in a compound document, supply pagination links in the
         corresponding links object.

        The following keys MUST be used for pagination links:

        first: the first page of data
        last: the last page of data
        prev: the previous page of data
        next: the next page of data

        We use page[offset] and page[limit], where
        offset is the number of records to offset by prior to returning resources
    '''

    def get_link(count, limit):
        result = request.scheme + '://' + request.host + request.path
        result += '?' + '&'.join(['{}={}'.format(k, v[0]) for k, v in request_args.items()] +
                                 ['page[offset]={}&page[limit]={}'.format(count, limit)])
        return result

    request_args = dict(request.args)
    page_offset = request.args.get('page[offset]', 0)

    try:
        del request_args['page[offset]']
        page_offset = int(page_offset)
    except:
        page_offset = 0

    limit = request.args.get('page[limit]', get_config('UNLIMITED'))
    try:
        del request_args['page[limit]']
        limit = int(limit)
    except:
        safrs.LOGGER.debug('Invalid page[limit]')

    page_base = int(page_offset / limit) * limit
    
    # Counting may take > 1s for a table with millions of records, depending on the storage engine :|
    # Make it configurable
    # With mysql innodb we can use following to retrieve the count:
    # select TABLE_ROWS from information_schema.TABLES where TABLE_NAME = 'TableName';
    #
    if SAFRSObject is None: # for backwards compatibility, ie. when not passed as an arg to paginate()
        count = object_query.count()
    else:
        count = SAFRSObject._s_count()
    if count is None:
        count = object_query.count()

    first_args = (0, limit)
    last_args = (int(int(count / limit) * limit), limit) # round down
    self_args = (page_base if page_base <= last_args[0] else last_args[0], limit)
    next_args = (page_offset + limit, limit) if page_offset + limit <= last_args[0] else last_args
    prev_args = (page_offset - limit, limit) if page_offset > limit else first_args

    links = {'first' : get_link(*first_args),\
             'self'  : get_link(page_offset, limit), # cfr. request.url
             'last'  : get_link(*last_args),\
             'prev'  : get_link(*prev_args),\
             'next'  : get_link(*next_args)}

    if last_args == self_args:
        del links['last']
    if first_args == self_args:
        del links['first']
    if next_args == last_args:
        del links['next']
    if prev_args == first_args:
        del links['prev']

    res_query = object_query.offset(page_offset).limit(limit)
    instances = res_query.all()
    return links, instances, count


def jsonapi_filter(safrs_object):
    '''
        Apply the request.args filters to the object
        returns a sqla query object
    '''

    filtered = []
    for arg, val in request.args.items():
        filter_attr = re.search('filter\[(\w+)\]', arg)
        if filter_attr:
            col_name = filter_attr.group(1)
            column = getattr(safrs_object, col_name)
            filtered.append(safrs_object.query.filter(column.in_(val.split(','))))

    if filtered:
        result = filtered[0].union_all(*filtered)
    else:
        result = safrs_object.query
    return result


def jsonapi_sort(object_query, safrs_object):
    '''
        http://jsonapi.org/format/#fetching-sorting
        sort by csv sort= values
    '''
    sort_columns = request.args.get('sort', None)
    if not sort_columns is None:
        for sort_column in sort_columns.split(','):
            if sort_column.startswith('-'):
                attr = getattr(safrs_object, sort_column[1:], None).desc()
                object_query = object_query.order_by(attr)
            else:
                attr = getattr(safrs_object, sort_column, None)
                object_query = object_query.order_by(attr)

    return object_query

def get_included(data, limit, include=''):
    '''
        return a set of included items

        http://jsonapi.org/format/#fetching-includes

        Inclusion of Related Resources
        Multiple related resources can be requested in a comma-separated list:
        An endpoint MAY return resources related to the primary data by default.
        An endpoint MAY also support an include request parameter to allow
        the client to customize which related resources should be returned.
        In order to request resources related to other resources,
        a dot-separated path for each relationship name can be specified
    '''
    result = set()

    if not include:
        return result

    if isinstance(data, (list, set)):
        for included in [get_included(obj, limit, include) for obj in data]:
            result = result.union(included)
        return result

    # When we get here, data has to be a SAFRSBase instance
    if not isinstance(data, SAFRSBase):
        return result
    instance = data

    # Multiple related resources can be requested in a comma-separated list
    includes = include.split(',')

    if INCLUDE_ALL in includes:
        includes += [r.key for r in instance._s_relationships]

    for include in set(includes):
        relationship = include.split('.')[0]
        nested_rel = None
        if '.' in include:
            nested_rel = '.'.join(include.split('.')[1:])
        if relationship in [r.key for r in instance._s_relationships]:
            included = getattr(instance, relationship)
            #if isinstance(included, sqlalchemy.orm.dynamic.AppenderBaseQuery):
            #    included = included.all()

            # relationship direction in (ONETOMANY, MANYTOMANY):
            if included and isinstance(included, SAFRSBase) and not included in result:
                result.add(included)
                continue
            if isinstance(included, sqlalchemy.orm.collections.InstrumentedList):
                # included should be an InstrumentedList
                included = included[:limit]
                result = result.union(included)
                continue
            if not included or included in result:
                continue
            try:
                # This works on sqlalchemy.orm.dynamic.AppenderBaseQuery
                included = included[:limit]
                result = result.union(included)
            except:
                safrs.LOGGER.critical('Failed to add included for {} (included: {} - {})'.format(relationship, type(included), included))
                result.add(included)

        if INCLUDE_ALL in includes:
            for nested_included in [get_included(result, limit) for obj in result]: # Removed recursion with get_included(result, limit, INCLUDE_ALL)
                result = result.union(nested_included)

        elif nested_rel:
            for nested_included in [get_included(result, limit, nested_rel) for obj in result]:
                result = result.union(nested_included)

    return result


def jsonapi_format_response(data, meta=None, links=None, errors=None, count=None):
    '''
    Create a response dict according to the json:api schema spec
    :param data : the objects that will be serialized
    :return: jsonapi formatted dictionary
    '''

    limit = request.args.get('page[limit]', get_config('UNLIMITED'))
    try:
        limit = int(limit)
    except Exception as exc:
        raise ValidationError('page[limit] error')
    meta['limit'] = limit
    meta['count'] = count

    jsonapi = dict(version='1.0')
    included = list(get_included(data, limit, include=request.args.get('include', safrs.SAFRS.DEFAULT_INCLUDED)))
    '''if count >= 0:
        included = jsonapi_format_response(included, {}, {}, {}, -1)'''
    result = dict(data=data)

    if errors:
        result['errors'] = errors
    if meta:
        result['meta'] = meta
    if jsonapi:
        result['jsonapi'] = jsonapi
    if links:
        result['links'] = links
    if included:
        result['included'] = included

    return result


class SAFRSRestAPI(Resource):
    '''
        Flask webservice wrapper for the underlying Resource Object:
        an sqla db model (SAFRSBase subclass : cls.SAFRSObject)

        This class implements HTTP Methods (get, post, put, delete, ...) and helpers

        http://jsonapi.org/format/#document-resource-objects
        A resource object MUST contain at least the following top-level members:
        - id
        - type

        In addition, a resource object MAY contain any of these top-level members:

        attributes: an attributes object representing some of the resource’s data.
        relationships: a relationships object describing relationships
                        between the resource and other JSON API resources.
        links: a links object containing links related to the resource.
        meta: a meta object containing non-standard meta-information
                about a resource that can not be represented as an attribute or relationship.

        e.g.
        {
            "id": "1f1c0e90-9e93-4242-9b8c-56ac24e505e4",
            "type": "car",
            "attributes": {
                "color": "red"
            },
            "relationships": {
                "driver": {
                    "data": {
                        "id": "55550e90-9e93-4242-9b8c-56ac24e505e5",
                        "type": "person"
                    }
                }
        }

        A resource object’s attributes and its relationships are collectively called its “fields”.
    '''

    SAFRSObject = None # Flask views will need to set this to the SQLAlchemy safrs.DB.Model class
    default_order = None # used by sqla order_by
    object_id = None

    def __init__(self, *args, **kwargs):
        '''
            - object_id is the function used to create the url parameter name
            (eg "User" -> "UserId" )
            - this parameter is used in the swagger endpoint spec,
            eg. /Users/{UserId} where the UserId parameter is the id of
            the underlying SAFRSObject.
        '''
        self.object_id = self.SAFRSObject.object_id


    def get(self, **kwargs):
        '''
            responses :
                404 :
                    description : Not Found
            ---
            HTTP GET: return instances
            If no id is given: return all instances
            If an id is given, get an instance by id
            If a method is given, call the method on the instance

            http://jsonapi.org/format/#document-top-level

            A JSON object MUST be at the root of every JSON API request
            and response containing data. This object defines a document’s “top level”.

            A document MUST contain at least one of the following top-level members:
            - data: the document’s “primary data”
            - errors: an array of error objects
            - meta: a meta object that contains non-standard meta-information.

            A document MAY contain any of these top-level members:
            - jsonapi: an object describing the server’s implementation
            - links: a links object related to the primary data.
            - included: an array of resource objects that are related
            to the primary data and/or each other (“included resources”).
        '''
        data = None
        meta = {}
        errors = None

        links = None

        id = kwargs.get(self.object_id, None)
        #method_name = kwargs.get('method_name','')

        if id:
            # Retrieve a single instance
            instance = self.SAFRSObject.get_instance(id)
            data = instance
            links = {'self' : request.url} 
            count = 1
            meta.update(dict(instance_meta=instance._s_meta()))

        else:
            # retrieve a collection
            instances = jsonapi_filter(self.SAFRSObject)
            instances = jsonapi_sort(instances, self.SAFRSObject)
            links, data, count = paginate(instances, self.SAFRSObject)

        result = jsonapi_format_response(data, meta, links, errors, count)
        return jsonify(result)

    def patch(self, **kwargs):
        '''
            responses:
                200 :
                    description : Accepted
                201 :
                    description: Created
                204 :
                    description : No Content
                403:
                    description : Forbidden
                404 :
                    description : Not Found
                409 :
                    description : Conflict
            ---
            Create or update the object specified by id
        '''
        id = kwargs.get(self.object_id, None)
        if not id:
            raise ValidationError('Invalid ID')

        req_json = request.get_json()
        if not isinstance(req_json, dict):
            raise ValidationError('Invalid Object Type')

        data = req_json.get('data')

        if not data or not isinstance(data, dict):
            raise ValidationError('Invalid Data Object')

        # Check that the id in the body is equal to the id in the url
        body_id = data.get('id', None)
        if body_id is None or \
        self.SAFRSObject.id_type.validate_id(id) != self.SAFRSObject.id_type.validate_id(body_id):
            raise ValidationError('Invalid ID')

        attributes = data.get('attributes', {})
        attributes['id'] = id
        # Create the object instance with the specified id and json data
        # If the instance (id) already exists, it will be updated with the data
        instance = self.SAFRSObject.get_instance(id)
        if not instance:
            raise ValidationError('Invalid ID')
        instance._s_patch(**attributes)

        # object id is the endpoint parameter, for example "UserId" for a User SAFRSObject
        obj_args = {instance.object_id : instance.jsonapi_id}
        # Retrieve the object json and return it to the client
        obj_data = self.get(**obj_args)
        response = make_response(obj_data, 201)
        # Set the Location header to the newly created object
        response.headers['Location'] = url_for(self.endpoint, **obj_args)
        return response

    def get_json(self):
        '''
            Extract and validate json request payload
        '''

        req_json = request.get_json()
        if not isinstance(req_json, dict):
            raise ValidationError('Invalid Object Type')

        return req_json


    def post(self, **kwargs):
        '''
            responses :
                403:
                    description : This implementation does not accept client-generated IDs
                201:
                    description: Created
                202:
                    description : Accepted
                404:
                    description : Not Found
                409:
                    description : Conflict
            ---
            http://jsonapi.org/format/#crud-creating
            Creating Resources
            A resource can be created by sending a POST request to a URL
            that represents a collection of resources.
            The request MUST include a single resource object as primary data.
            The resource object MUST contain at least a type member.

            If a relationship is provided in the relationships member of the resource object,
            its value MUST be a relationship object with a data member.
            The value of this key represents the linkage the new resource is to have.

            Response:
            403: This implementation does not accept client-generated IDs
            201: Created
            202: Accepted
            404: Not Found
            409: Conflict

            Location Header identifying the location of the newly created resource
            Body : created object

            TODO:
            409 Conflict
              A server MUST return 409 Conflict when processing a POST request
              to create a resource with a client-generated ID that already exists.
              A server MUST return 409 Conflict when processing a POST request
              in which the resource object’s type is not among the type(s) that
              constitute the collection represented by the endpoint.
              A server SHOULD include error details and provide enough
              information to recognize the source of the conflict.
        '''
        payload = self.get_json()
        method_name = payload.get('meta', {}).get('method', None)

        id = kwargs.get(self.object_id, None)
        if id is not None:
            # Treat this request like a patch
            # this isn't really jsonapi-compliant:
            # "A server MUST return 403 Forbidden in response to an
            # unsupported request to create a resource with a client-generated ID"
            try:
                response = self.patch(**kwargs)
            except Exception as exc:
                raise GenericError('POST failed')

        else:
            # Create a new instance of the SAFRSObject
            data = payload.get('data')
            if data is None:
                raise ValidationError('Request contains no data')
            if not isinstance(data, dict):
                raise ValidationError('data is not a dict object')

            obj_type = data.get('type', None)
            if not obj_type: # or type..
                raise ValidationError('Invalid type member')

            attributes = data.get('attributes', {})
            # Create the object instance with the specified id and json data
            # If the instance (id) already exists, it will be updated with the data
            instance = self.SAFRSObject(**attributes)

            if not instance.db_commit:
                #
                # The item has not yet been added/commited by the SAFRSBase,
                # in that case we have to do it ourselves
                #
                safrs.DB.session.add(instance)
                try:
                    safrs.DB.session.commit()
                except sqlalchemy.exc.SQLAlchemyError as exc:
                    # Exception may arise when a db constrained has been violated
                    # (e.g. duplicate key)
                    safrs.LOGGER.warning(str(exc))
                    raise GenericError(str(exc))

             # object_id is the endpoint parameter, for example "UserId" for a User SAFRSObject
            obj_args = {instance.object_id : instance.jsonapi_id}
            # Retrieve the object json and return it to the client
            obj_data = self.get(**obj_args)
            response = make_response(obj_data, 201)
            # Set the Location header to the newly created object
            response.headers['Location'] = url_for(self.endpoint, **obj_args)

        return response


    def delete(self, **kwargs):
        '''

            responses:
                202 :
                    description: Accepted
                204 :
                    description: No Content
                200 :
                    description: Success
                403 :
                    description: Forbidden
                404 :
                    description: Not Found

            ---
            Delete an object by id or by filter

            http://jsonapi.org/format/1.1/#crud-deleting:
            Responses :
                202 : Accepted
                If a deletion request has been accepted for processing,
                but the processing has not been completed by the time the server
                responds, the server MUST return a 202 Accepted status code.

                204 No Content
                A server MUST return a 204 No Content status code if a deletion
                request is successful and no content is returned.

                200 OK
                A server MUST return a 200 OK status code if a deletion request
                is successful and the server responds with only top-level meta data.

                404 NOT FOUND
                A server SHOULD return a 404 Not Found status code
                if a deletion request fails due to the resource not existing.
        '''

        id = kwargs.get(self.object_id, None)

        if id:
            instance = self.SAFRSObject.get_instance(id)
            safrs.DB.session.delete(instance)
        else:
            raise NotFoundError(id, status_code=404)

        return jsonify({})

    def call_method_by_name(self, instance, method_name, args):
        '''
            Call the instance method specified by method_name
        '''

        method = getattr(instance, method_name, False)

        if not method:
            # Only call methods for Campaign and not for superclasses (e.g. safrs.DB.Model)
            raise ValidationError('Invalid method "{}"'.format(method_name))
        if not is_public(method):
            raise ValidationError('Method is not public')

        if not args: 
            args = {}

        result = method(**args)
        return result

    def get_instances(self, filter, method_name, sort, search=''):
        '''
            Get all instances. Subclasses may want to override this
            (for example to sort results)
        '''

        if method_name:
            method(**args)

        instances = self.SAFRSObject.query.filter_by(**filter).order_by(None)

        return instances


class SAFRSRestMethodAPI(Resource, object):
    '''
        Route wrapper for the underlying SAFRSBase jsonapi_rpc

        Only HTTP POST is supported
    '''

    SAFRSObject = None # Flask views will need to set this to the SQLAlchemy safrs.DB.Model class
    method_name = None

    def __init__(self, *args, **kwargs):
        '''
            -object_id is the function used to create the url parameter name
            (eg "User" -> "UserId" )
            -this parameter is used in the swagger endpoint spec,
            eg. /Users/{UserId} where the UserId parameter is the id of the underlying SAFRSObject.
        '''
        self.object_id = self.SAFRSObject.object_id

    def post(self, **kwargs):
        '''
            responses :
                403:
                    description : This implementation does not accept client-generated IDs
                201:
                    description: Created
                202:
                    description : Accepted
                404:
                    description : Not Found
                409:
                    description : Conflict
            ---
            HTTP POST: apply actions, return 200 regardless
        '''
        id = kwargs.get(self.object_id, None)

        if id != None:
            instance = self.SAFRSObject.get_instance(id)
            if not instance:
                # If no instance was found this means the user supplied
                # an invalid ID
                raise ValidationError('Invalid ID')

        else:
            # No ID was supplied, apply method to the class itself
            instance = self.SAFRSObject

        method = getattr(instance, self.method_name, None)

        if not method:
            # Only call methods for Campaign and not for superclasses (e.g. safrs.DB.Model)
            raise ValidationError('Invalid method "{}"'.format(method_name))
        if not is_public(method):
            raise ValidationError('Method is not public')

        args = dict(request.args)
        json_data = request.get_json({})
        if json_data:
            args = json_data.get('meta', {}).get('args', {})

        safrs.LOGGER.debug('method {} args {}'.format(self.method_name, args))

        result = method(**args)

        if isinstance(result, SAFRSFormattedResponse):
            response = result
        else:
            response = {'meta': {'result': result}}

        return jsonify(response) # 200 : default


    def get(self, **kwargs):
        '''
            responses :
                404 :
                    description : Not Found
                403 :
                    description : Forbidden

            ---
        '''

        id = kwargs.get(self.object_id, None)

        if id != None:
            instance = self.SAFRSObject.get_instance(id)
            if not instance:
                # If no instance was found this means the user supplied
                # an invalid ID
                raise ValidationError('Invalid ID')

        else:
            # No ID was supplied, apply method to the class itself
            instance = self.SAFRSObject

        method = getattr(instance, self.method_name, None)

        if not method:
            # Only call methods for Campaign and not for superclasses (e.g. safrs.DB.Model)
            raise ValidationError('Invalid method "{}"'.format(method_name))
        if not is_public(method):
            raise ValidationError('Method is not public')

        args = dict(request.args)
        safrs.LOGGER.debug('method {} args {}'.format(self.method_name, args))

        result = method(**args)

        response = {'meta': {'result' : result}}

        return jsonify(response) # 200 : default

class SAFRSRelationshipObject:
    '''
        Relationship object
    '''

    __tablename__ = 'tabname'
    __name__ = 'name'

    @classmethod
    def get_swagger_doc(cls, http_method):
        '''
            Create a swagger api model based on the sqlalchemy schema
            if an instance exists in the DB, the first entry is used as example
        '''
        body = {}
        responses = {}
        object_name = cls.__name__

        object_model = {}
        responses = {'200': {\
                             'description' : '{} object'.format(object_name),\
                             'schema': object_model\
                            }\
                    }

        if http_method == 'post':
            responses = {'200' : {\
                                  'description' : 'Success',\
                                 }\
                        }

        if http_method == 'get':
            responses = {'200' : {\
                                  'description' : 'Success',\
                                 }\
                        }
            #responses['200']['schema'] = {'$ref': '#/definitions/{}'.format(object_model.__name__)}

        return body, responses


class SAFRSRestRelationshipAPI(Resource):
    '''
        Flask webservice wrapper for the underlying sqla relationships db model

        The endpoint url is of the form
        "/Parents/{ParentId}/children/{ChildId}"
        (cfr RELATIONSHIP_URL_FMT in API.expose_relationship)
        where "children" is the relationship attribute of the parent

        3 types of relationships (directions) exist in the sqla orm:
        MANYTOONE ONETOMANY MANYTOMANY

        Following attributes are set on this class:
            - SAFRSObject: the sqla object which has been set with the type
             constructor in expose_relationship
            - parent_class: class of the parent ( e.g. Parent, __tablename__ : Parents )
            - child_class : class of the child
            - rel_name : name of the relationship ( e.g. children )
            - parent_object_id : url parameter name of the parent ( e.g. {ParentId} )
            - child_object_id : url parameter name of the child ( e.g. {ChildId} )

        http://jsonapi.org/format/#crud-updating-relationships

        Updating To-Many Relationships
        A server MUST respond to PATCH, POST, and DELETE requests to a URL
        from a to-many relationship link as described below.

        For all request types, the body MUST contain a data member
        whose value is an empty array or an array of resource identifier objects.

        If a client makes a PATCH request to a URL from a to-many relationship link,
        the server MUST either completely replace every member of the relationship,
        return an appropriate error response if some resources can not be
        found or accessed, or return a 403 Forbidden response if complete
        replacement is not allowed by the server.
    '''

    SAFRSObject = None

    #pylint: disable=unused-argument
    def __init__(self, *args, **kwargs):

        self.parent_class = self.SAFRSObject.relationship.parent.class_
        self.child_class = self.SAFRSObject.relationship.mapper.class_
        self.rel_name = self.SAFRSObject.relationship.key
        # The object_ids are the ids in the swagger path e.g {FileId}
        self.parent_object_id = self.parent_class.object_id
        self.child_object_id = self.child_class.object_id

        if self.parent_object_id == self.child_object_id:
            # see expose_relationship: if a relationship consists of
            # two same objects, the object_id should be different (i.e. append "2")
            self.child_object_id += '2'

    def get(self, **kwargs):
        '''
            ---
            Retrieve a relationship or list of relationship member ids

            http://jsonapi.org/format/#fetching-relationships-responses :
            A server MUST respond to a successful request to fetch a
            relationship with a 200 OK response.The primary data in the response
            document MUST match the appropriate value for resource linkage.
            The top-level links object MAY contain self and related links,
            as described above for relationship objects.
        '''
        parent, relation = self.parse_args(**kwargs)
        child_id = kwargs.get(self.child_object_id)

        if child_id:
            child = self.child_class.get_instance(child_id)
            # If {ChildId} is passed in the url, return the child object
            # there's a difference between to-one and -to-many relationships:
            if isinstance(relation, SAFRSBase):
                result = [child]
            elif child in relation:
                # item is in relationship, return the child
                result = [child]
            else:
                return 'Not Found', 404
        #elif type(relation) == self.child_class: # ==>
        elif self.SAFRSObject.relationship.direction == MANYTOONE:
            result = relation
        else:
            # No {ChildId} given:
            # return a list of all relationship items
            result = [item for item in relation]

        if self.SAFRSObject.relationship.direction == MANYTOONE:
            meta = {'direction' : 'TOONE'}
        else:
            meta = {'direction' : 'TOMANY'}

        result = {'data' : result, 'links' : {'self' : request.url}, 'meta' : meta}
        return jsonify(result)

    # Relationship patching
    def patch(self, **kwargs):
        '''
            responses:
                200 :
                    description : Accepted
                201 :
                    description: Created
                204 :
                    description : No Content
                403:
                    description : Forbidden
                404 :
                    description : Not Found
                409 :
                    description : Conflict
            ----
            Update or create a relationship child item
            to be used to create or update one-to-many mappings
            but also works for many-to-many etc.

            # Updating To-One Relationships

            http://jsonapi.org/format/#crud-updating-to-one-relationships:
            A server MUST respond to PATCH requests to a URL
            from a to-one relationship link as described below

            The PATCH request MUST include a top-level member named data containing one of:
            a resource identifier object corresponding to the new related resource.
            null, to remove the relationship.
        '''
        parent, relation = self.parse_args(**kwargs)
        json_reponse = request.get_json()
        if not isinstance(json_reponse, dict):
            raise ValidationError('Invalid Object Type')

        data = json_reponse.get('data')
        relation = getattr(parent, self.rel_name)
        obj_args = {self.parent_object_id : parent.jsonapi_id}

        if isinstance(data, dict):
            # => Update TOONE Relationship
            # TODO!!!
            if self.SAFRSObject.relationship.direction != MANYTOONE:
                raise GenericError('To PATCH a TOMANY relationship you should provide a list')
            child = self.child_class.get_instance(data.get('id', None))
            setattr(parent, self.rel_name, child)
            obj_args[self.child_object_id] = child.jsonapi_id
            '''
                http://jsonapi.org/format/#crud-updating-to-many-relationships

                If a client makes a PATCH request to a URL from a to-many relationship link,
                the server MUST either completely replace every member of the relationship,
                return an appropriate error response if some resourcescan not be found
                or accessed, or return a 403 Forbidden response if complete
                replacement is not allowed by the server.
            '''
        elif isinstance(data, list):
            if self.SAFRSObject.relationship.direction == MANYTOONE:
                raise GenericError('To PATCH a MANYTOONE relationship you \
                should provide a dictionary instead of a list')
            # first remove all items, then append the new items
            # if the relationship has been configured with lazy="dynamic"
            # then it is a subclass of AppenderBaseQuery and
            # we should empty the relationship by setting it to []
            # otherwise it is an instance of InstrumentedList and we have to empty it
            # ( we could loop all items but this is slower for large collections )
            tmp_rel = []
            for child in data:
                if not isinstance(child, dict):
                    raise ValidationError('Invalid data object')
                child_instance = self.child_class.get_instance(child['id'])
                tmp_rel.append(child_instance)

            if isinstance(relation, sqlalchemy.orm.collections.InstrumentedList):
                relation[:] = tmp_rel
            else:
                setattr(parent, self.rel_name, tmp_rel)

        elif data is None:
            # { data : null } //=> clear the relationship
            if self.SAFRSObject.relationship.direction == MANYTOONE:
                child = getattr(parent, self.SAFRSObject.relationship.key)
                if child:
                    pass
                setattr(parent, self.rel_name, None)
            else:
                #
                # should we allow this??
                # maybe we just want to raise an error here ...???
                setattr(parent, self.rel_name, [])
        else:
            raise ValidationError('Invalid Data Object Type')

        if data is None:
            # item removed from relationship => 202 accepted
            # TODO: add response to swagger
            # add meta?

            response = {}, 200
        else:
            obj_data = self.get(**obj_args)
            # Retrieve the object json and return it to the client
            response = make_response(obj_data, 201)
            # Set the Location header to the newly created object
            # todo: set location header
            #response.headers['Location'] = url_for(self.endpoint, **obj_args)
        return response

    def post(self, **kwargs):
        '''
            responses :
                403:
                    description : This implementation does not accept client-generated IDs
                201:
                    description: Created
                202:
                    description : Accepted
                404:
                    description : Not Found
                409:
                    description : Conflict
            ---
            Add a child to a relationship
        '''
        errors = []
        kwargs['require_child'] = True
        parent, relation = self.parse_args(**kwargs)

        json_response = request.get_json()
        if not isinstance(json_response, dict):
            raise ValidationError('Invalid Object Type')
        data = json_response.get('data')

        if self.SAFRSObject.relationship.direction == MANYTOONE:
            if len(data) == 0:
                setattr(parent, self.SAFRSObject.relationship.key, None)
            if len(data) > 1:
                raise ValidationError('Too many items for a MANYTOONE relationship', 403)
            child_id = data[0].get('id')
            child_type = data[0].get('type')
            if not child_id or not child_type:
                raise ValidationError('Invalid data payload', 403)

            if child_type != self.child_class.__name__:
                raise ValidationError('Invalid type', 403)

            child = self.child_class.get_instance(child_id)
            setattr(parent, self.SAFRSObject.relationship.key, child)
            result = [child]

        else: # direction is TOMANY => append the items to the relationship
            for item in data:
                if not isinstance(json_response, dict):
                    raise ValidationError('Invalid data type')
                child_id = item.get('id', None)
                if child_id is None:
                    errors.append('no child id {}'.format(data))
                    safrs.LOGGER.error(errors)
                    continue
                child = self.child_class.get_instance(child_id)

                if not child:
                    errors.append('invalid child id {}'.format(child_id))
                    safrs.LOGGER.error(errors)
                    continue
                if not child in relation:
                    relation.append(child)
            result = [item for item in relation]

        return jsonify({'data' : result})

    def delete(self, **kwargs):
        '''
            responses:
                202 :
                    description: Accepted
                204 :
                    description: No Content
                200 :
                    description: Success
                403 :
                    description: Forbidden
                404 :
                    description: Not Found
            ----
            Remove an item from a relationship
        '''

        kwargs['require_child'] = True

        parent, relation = self.parse_args(**kwargs)
        child_id = kwargs.get(self.child_object_id, None)
        child = self.child_class.get_instance(child_id)
        if child in relation:
            relation.remove(child)
        else:
            safrs.LOGGER.warning('Child not in relation')

        return jsonify({})

    def parse_args(self, **kwargs):
        '''
            Parse relationship args
            An error is raised if the parent doesn't exist.
            An error is raised if the child doesn't exist and the
            "require_child" argument is set in kwargs,

            Returns
                parent, child, relation
        '''

        parent_id = kwargs.get(self.parent_object_id, None)
        if parent_id is None:
            raise ValidationError('Invalid Parent Id')

        parent = self.parent_class.get_instance(parent_id)
        relation = getattr(parent, self.rel_name)

        return parent, relation
