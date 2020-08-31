from __future__ import (absolute_import, division, print_function)
import hashlib
from textwrap import wrap
import base64
import time
import re

from ansible_collections.serverscom.sc_api.plugins.module_utils.sc_api import (
    SCBaseError,
    APIError404,
    APIError409,
    DEFAULT_API_ENDPOINT,
    ApiHelper,
    ScApi
)

__metaclass__ = type


CHANGED = True
NOT_CHANGED = False


class ModuleError(SCBaseError):
    def __init__(self, msg):
        self.msg = msg

    def fail(self):
        return {
            'failed': True,
            'msg': self.msg
        }


class TimeOutError(ModuleError):
    def __init__(self, msg, timeout):
        self.msg = msg
        self.timeout = timeout

    def fail(self):
        return {
            'failed': True,
            'timeout': self.timeout,
            'msg': self.msg
        }


class ApiSimpleGet(object):
    ''' Generic class for modules with single GET and no additional options.'''

    path = None

    query_parameters = None

    def build_path(self):
        if not self.path:
            raise AssertionError("Class should have path defined.")
        return self.path

    def build_query_parameters(self):
        return self.query_parameters

    def __init__(self, endpoint, token):
        self.api_helper = ApiHelper(token, endpoint)

    def process_response(self, response):
        response['changed'] = False
        return response

    def run(self):
        return self.process_response(
            self.api_helper.make_get_request(
                self.build_path(),
                self.build_query_parameters()
            )
        )


class ApiMultipageGet(ApiSimpleGet):
    ''' Generic class for modules with multipage GET and no options.'''
    response_key = None

    def process_response(self, response):
        if not self.response_key:
            raise AssertionError("Class should have response_key defined.")
        return {
            'changed': False,
            self.response_key: list(response)
        }

    def run(self):
        return self.process_response(
            self.api_helper.make_multipage_request(
                self.build_path(),
                self.build_query_parameters()
            )
        )


class ScDedicatedServerInfo(object):
    def __init__(self, endpoint, token, name, fail_on_absent):
        self.api = ScApi(token, endpoint)
        self.server_id = name
        self.fail_on_absent = fail_on_absent

    @staticmethod
    def _is_server_ready(server_info):
        if (
            server_info.get('status') == 'active' and
            server_info.get('power_status') == 'powered_on' and
            server_info.get('operational_status') == 'normal'
        ):
            return True
        else:
            return False

    def run(self):
        try:
            server_info = self.api.get_dedicated_server(self.server_id)
        except APIError404 as e:
            if self.fail_on_absent:
                raise e
            return {
                'changed': False,
                'found': False,
                'ready': False
            }
        module_output = server_info
        module_output['found'] = True
        module_output['ready'] = self._is_server_ready(server_info)
        module_output['changed'] = False
        return module_output


class ScBaremetalServersInfo(ApiMultipageGet):
    path = '/hosts'
    response_key = 'baremetal_servers'


class ScBaremetalLocationsInfo(object):
    def __init__(self, endpoint, token,
                 search_pattern, required_features):
        self.search_pattern = search_pattern
        self.required_features = required_features
        self.api_helper = ApiHelper(token, endpoint)

    @staticmethod
    def location_features(location):
        features = set(location['supported_features'])
        for key, value in location.items():
            # fiter out both non-feature things like name, and
            # disabled features,
            if value is True:
                features.add(key)
        return features

    def locations(self):
        all_locations = list(self.api_helper.make_multipage_request(
            path='/locations',
            query_parameters={'search_pattern': self.search_pattern}
        ))
        locations = []
        if self.required_features:
            for loc in all_locations:
                feature_match = not (
                    set(self.required_features) - self.location_features(loc)
                )
                if feature_match:
                    locations.append(loc)

        else:
            locations = all_locations
        return locations

    def run(self):
        ret_data = {'changed': False}
        ret_data["locations"] = self.locations()
        return ret_data


class ScCloudComputingRegionsInfo(object):
    def __init__(self, endpoint, token,
                 search_pattern):
        self.search_pattern = search_pattern
        self.api_helper = ApiHelper(token, endpoint)

    @staticmethod
    def location_features(location):
        features = set(location['supported_features'])
        for key, value in location.items():
            # fiter out both non-feature things like name, and
            # disabled features,
            if value is True:
                features.add(key)
        return features

    def regions(self):
        return self.api_helper.make_multipage_request(
            '/cloud_computing/regions'
        )

    def search(self, regions):
        for region in regions:
            if not self.search_pattern:
                yield region
            else:
                if self.search_pattern.lower() in region['name'].lower() or \
                   self.search_pattern.lower() in region['code'].lower():
                    yield region

    def run(self):
        ret_data = {'changed': False}
        ret_data['regions'] = list(
            self.search(self.regions())
        )
        return ret_data


class ScSshKey(object):
    def __init__(
        self, endpoint, token, state, name, fingerprint,
        public_key, replace, checkmode
    ):
        self.partial_match = []
        self.full_match = []
        self.any_match = []
        self.api_helper = ApiHelper(token, endpoint)
        self.checkmode = checkmode
        self.replace = replace
        self.state = state
        self.key_name = name
        self.public_key = public_key
        self.fingerprint = fingerprint
        if public_key:
            self.fingerprint = self.extract_fingerprint(public_key)
            if fingerprint and self.fingerprint != fingerprint:
                raise ModuleError(
                    msg='Fingerprint does not match public_key'
                )
        if state == 'absent':
            if not any([fingerprint, name, public_key]):
                raise ModuleError(
                    'Need at least one of name, fingerprint, public_key '
                    'for state=absent'
                )
        if state == 'present':
            if not public_key:
                raise ModuleError(
                    'Need public_key for state=present'
                )
            if not name:
                raise ModuleError(
                    'Need name for state=present'
                )

    @staticmethod
    def extract_fingerprint(public_key):
        parts = public_key.split()
        # real key is the largest word in the line
        parts.sort(key=len, reverse=True)
        the_key = base64.decodebytes(parts[0].encode('ascii'))
        digest = hashlib.md5(the_key).hexdigest()
        fingerprint = ':'.join(wrap(digest, 2))
        return fingerprint

    def get_ssh_keys(self):
        return self.api_helper.make_multipage_request('/ssh_keys')

    @staticmethod
    def classify_matching_keys(key_list, name, fingerprint):
        full_match = []
        partial_match = []
        any_match = []
        for key in key_list:
            if key['name'] == name or key['fingerprint'] == fingerprint:
                any_match.append(key)
                if key['name'] == name and key['fingerprint'] == fingerprint:
                    full_match.append(key)
                else:
                    partial_match.append(key)
        return (full_match, partial_match, any_match)

    def add_key(self):
        if not self.checkmode:
            self.api_helper.make_post_request(
                path='/ssh_keys',
                body=None,
                query_parameters={
                    'name': self.key_name, 'public_key': self.public_key
                },
                good_codes=[201]
            )

    def delete_keys(self, key_list):
        if not self.checkmode:
            for key in key_list:
                self.api_helper.make_delete_request(
                    path=f'/ssh_keys/{key["fingerprint"]}',
                    body=None,
                    query_parameters=None,
                    good_codes=[204]
                )

    def state_absent(self):
        # import epdb
        # epdb.serve()
        if not self.any_match:
            return NOT_CHANGED
        self.delete_keys(self.any_match)
        return CHANGED

    def state_present(self):
        changed = NOT_CHANGED
        if self.full_match and not self.partial_match:
            return NOT_CHANGED
        if self.partial_match and not self.replace:
            raise ModuleError(
                'Error: Partial match found and no replace option. '
                f'Partially matching keys: {repr(self.partial_match)}'
            )
        if self.partial_match and self.replace:
            self.delete_keys(self.partial_match)
            changed = CHANGED
        if not self.full_match:
            self.add_key()
            changed = CHANGED
        return changed

    def run(self):
        self.full_match, self.partial_match, self.any_match = \
            self.classify_matching_keys(
                self.get_ssh_keys(), self.key_name, self.fingerprint
            )
        if self.state == 'absent':
            changed = self.state_absent()
        if self.state == 'present':
            changed = self.state_present()
        return {'changed': changed}


class ScSshKeysInfo(ApiMultipageGet):
    path = '/ssh_keys'
    response_key = 'ssh_keys'


class ScDedicatedServerReinstall(object):
    def __init__(
        self,
        endpoint,
        token,
        server_id,
        hostname,
        drives_layout_template,
        drives_layout,
        operating_system_id,
        ssh_keys,
        ssh_key_name,
        wait,
        update_interval,
        checkmode
    ):
        if wait:
            if int(wait) < int(update_interval):
                raise ModuleError(
                    f"Update interval ({update_interval}) is longer "
                    f"than wait time ({wait}"
                )
        self.api_helper = ApiHelper(token, endpoint)
        self.old_server_data = None
        self.server_id = server_id
        self.hostname = self.get_hostname(hostname)
        self.drives_layout = self.get_drives_layout(drives_layout,
                                                    drives_layout_template)
        self.operating_system_id = self.get_operating_system_id(
            operating_system_id
        )
        self.ssh_keys = self.get_ssh_keys(ssh_keys, ssh_key_name)
        self.wait = wait
        self.update_interval = update_interval
        self.checkmode = checkmode

    def get_server_data(self):
        if self.old_server_data:
            return
        self.old_server_data = self.api_helper.make_get_request(
            path=f'/hosts/dedicated_servers/{self.server_id}',
            query_parameters=None
        )

    def get_ssh_key_by_name(self, ssh_key_name):
        api_keys = self.api_helper.make_multipage_request('/ssh_keys')
        for key in api_keys:
            if key['name'] == ssh_key_name:
                return key['fingerprint']
        raise ModuleError(
            f'Unable to find registered ssh key with name "{ssh_key_name}"'
        )

    def get_hostname(self, hostname):
        if hostname:
            return hostname
        self.get_server_data()
        if 'title' not in self.old_server_data:
            raise ModuleError(
                "Unable to retrive old title for the server. "
                "use hostname option to specify the hostname for reinstall."
            )
        return self.old_server_data['title']

    def get_operating_system_id(self, operating_system_id):
        if operating_system_id:
            return operating_system_id
        self.get_server_data()
        cfg = self.old_server_data.get('configuration_details')
        if not cfg or 'operating_system_id' not in cfg:
            raise ModuleError(
                "no operating_system_id was given, and unable to get old"
                "operating_system_id"
            )
        return cfg['operating_system_id']

    def get_ssh_keys(self, ssh_keys, ssh_key_name):
        if ssh_keys:
            return ssh_keys
        if not ssh_key_name:
            return []
        key = self.get_ssh_key_by_name(ssh_key_name)
        return [key]

    @staticmethod
    def get_drives_layout(layout, template):
        partitions_template = [
            {
                "target": "/boot",
                "size": 500,
                "fill": False, "fs": "ext4"
            },
            {
                "target": "swap",
                "size": 4096,
                "fill": False
            },
            {
                "target": "/",
                "fill": True,
                "fs": "ext4"
            }
        ]
        rai1_simple = [{
            'slot_positions': [0, 1],
            'raid': 1,
            'partitions': partitions_template
        }]
        raid0_simple = [{
            'slot_positions': [0],
            'raid': 0,
            'partitions': partitions_template
        }]
        templates = {
            'raid1-simple': rai1_simple,
            'raid0-simple': raid0_simple
        }
        if layout:
            return layout
        if template not in templates:
            raise ModuleError("Invalid drives_layout_template.")
        else:
            return templates[template]

    def make_request_body(self):
        return {
            'hostname': self.hostname,
            'operating_system_id': self.operating_system_id,
            'ssh_key_fingerprints': self.ssh_keys,
            'drives': {
                'layout': self.drives_layout,
            }
        }

    def wait_for_server(self):
        ready = False
        start_time = time.time()
        elapsed = 0
        while not ready:
            time.sleep(self.update_interval)
            elapsed = time.time() - start_time
            if elapsed > self.wait:
                raise TimeOutError(
                    msg="Server is not ready.",
                    timeout=elapsed
                )
            server_info = self.api_helper.make_get_request(
                path=f'/hosts/dedicated_servers/{self.server_id}',
                query_parameters=None
            )
            ready = ScDedicatedServerInfo._is_server_ready(server_info)
        server_info['ready'] = True
        server_info['elapsed'] = elapsed
        return server_info

    def run(self):
        if self.checkmode:
            return {'changed': True}
        result = self.api_helper.make_post_request(
            path=f'/hosts/dedicated_servers/{self.server_id}/reinstall',
            body=self.make_request_body(),
            query_parameters=None,
            good_codes=[202]
        )
        if self.wait:
            result = self.wait_for_server()
        result['changed'] = True
        return result


class ScCloudComputingFlavorsInfo(ApiMultipageGet):

    response_key = 'cloud_flavors'

    def __init__(self, token, endpoint, region_id):
        self.api_helper = ApiHelper(token, endpoint)
        self.region_id = region_id

    def build_path(self):
        return f'/cloud_computing/regions/{self.region_id}/flavors'


class ScCloudComputingImagesInfo(ApiMultipageGet):

    response_key = 'cloud_images'

    def __init__(self, token, endpoint, region_id):
        self.api_helper = ApiHelper(token, endpoint)
        self.region_id = region_id

    def build_path(self):
        return f'/cloud_computing/regions/{self.region_id}/images'


class ScCloudComputingInstancesInfo(ApiMultipageGet):

    response_key = 'cloud_instances'
    path = '/cloud_computing/instances'

    def __init__(self, token, endpoint, region_id):
        self.api_helper = ApiHelper(token, endpoint)
        if region_id:
            self.query_parameters = {
                'region_id': region_id
            }


class ScCloudComputingInstanceInfo(ApiSimpleGet):
    def __init__(self, token, endpoint, instance_id):
        self.api_helper = ApiHelper(token, endpoint)
        self.path = f'/cloud_computing/instances/{instance_id}'


class ScCloudComputingOpenstackCredentials(ApiSimpleGet):

    def __init__(self, token, endpoint, region_id):
        self.api_helper = ApiHelper(token, endpoint)
        self.region_id = region_id

    def build_path(self):
        return f'/cloud_computing/regions/{self.region_id}/credentials'


class ScCloudComputingInstanceReinstall(object):
    def __init__(
        self,
        endpoint,
        token,
        instance_id,
        image_id,
        wait_for_active,
        wait_for_rebuilding,
        update_interval,
        checkmode
    ):
        if not wait_for_rebuilding and wait_for_active:
            raise ModuleError(
                f'Unsupported mode: wait_for_rebuilding={wait_for_rebuilding} '
                f'and wait_for_active={wait_for_active}.'
            )
        self.api_helper = ApiHelper(token, endpoint)
        self.instance_id = instance_id
        self.image_id = self.get_image_id(image_id)
        self.wait_for_active = wait_for_active
        self.wait_for_rebuilding = wait_for_rebuilding
        self.update_interval = update_interval
        self.checkmode = checkmode

    def get_instance(self):
        return self.api_helper.make_get_request(
            path=f'/cloud_computing/instances/{self.instance_id}',
            query_parameters=None
        )

    def get_image_id(self, image_id):
        if image_id:
            return image_id
        old_image_id = self.get_instance().get('image_id')
        if not old_image_id:
            raise ModuleError(
                "Can't find old image id of instance. "
                "Use image_id option."
            )
        return old_image_id

    def wait_for(self, desired_status, timeout):
        if not timeout:
            return {}
        start_time = time.time()
        elapsed = 0
        while elapsed < timeout:
            time.sleep(timeout)
            elapsed = time.time() - start_time
            instance = self.get_instance()
            status = instance.get('status')
            if not status:
                ModuleError("Status is not defined in API answer.")
            if status == desired_status:
                return instance
        raise TimeOutError(
            f'Timeout waiting for {desired_status}, '
            f'last status was {status}',
            timeout=timeout
        )

    def run(self):
        if self.checkmode:
            instance = self.get_instance()
            instance['changed'] = True
            return instance
        instance = self.api_helper.make_post_request(
            path=f'/cloud_computing/instances/{self.instance_id}/reinstall',
            body=None,
            query_parameters={
                'image_id': self.image_id
            },
            good_codes=[202]
        )
        instance = self.wait_for('REBUILDING', self.wait_for_rebuilding)
        instance = self.wait_for('ACTIVE', self.wait_for_active)
        instance['changed'] = True
        return instance


class ScCloudComputingInstance(object):
    """Common methods for ScCloudComputingInstanceCreate and
       ScCloudComputingInstanceDelete.
    """

    def region_query(self):
        if self.region_id:
            return {'region_id': self.region_id}
        else:
            return {}

    def find_instance_by_name(self, name):
        instances = self.api_helper.make_multipage_request(
            path='/cloud_computing/instances',
            query_parameters=self.region_query()
        )
        found = []
        for instance in instances:
            if instance['name'] == name:
                found.append(instance)
        if len(found) > 1:
            raise ModuleError(
                f'Multiple instances found with name {name}'
            )
        if len(found) == 0:
            return None
        return found[0]

    def find_instance_by_id(self, instance_id):
        return self.api_helper.make_get_request(
            path=f'/cloud_computing/instances/{instance_id}',
            query_parameters=None
        )

    def find_instance(self):
        """Search for instance. Return None if not found."""
        try:
            if self.instance_id:
                return self.find_instance_by_id(self.instance_id)
            elif self.name:
                return self.find_instance_by_name(self.name)
            else:
                raise ModuleError("No instance_id or name was specified")
        except APIError404:
            return None


class ScCloudComputingInstanceCreate(ScCloudComputingInstance):
    def __init__(
        self,
        endpoint, token,
        region_id, name,
        image_id, image_regexp,
        flavor_id, flavor_name,
        gpn_enabled, ipv6_enabled,
        ssh_key_fingerprint, ssh_key_name,
        backup_copies,
        wait, update_interval,
        checkmode
    ):
        self.checkmode = checkmode
        self.api_helper = ApiHelper(token, endpoint)
        if region_id is None:
            raise ModuleError("region_id is mandatory for state=present.")
        self.region_id = region_id
        if not name:
            raise ModuleError("Name is mandatory for state=present.")
        self.name = name
        self.instance_id = None
        self.flavor_id = self.get_flavor_id(flavor_id, flavor_name)
        self.image_id = self.get_image_id(image_id, image_regexp)
        self.gpn_enabled = gpn_enabled
        self.ipv6_enabled = ipv6_enabled
        self.ssh_key_fingerprint = self.get_ssh_key_fingerprint(
            ssh_key_fingerprint,
            ssh_key_name
        )
        self.backup_copies = backup_copies
        self.wait = wait
        self.update_interval = update_interval
        self.checkmode = checkmode

    def get_ssh_key_fingerprint(self, ssh_key_fingerprint, ssh_key_name):
        if ssh_key_fingerprint:
            return ssh_key_fingerprint
        if ssh_key_name:
            ssh_keys = self.api_helper.make_multipage_request(
                path='/ssh_keys',
                query_parameters=None
            )
            for key in ssh_keys:
                if key['name'] == ssh_key_name:
                    return key['fingerprint']
            raise ModuleError(f"Unable to find ssh key {ssh_key_name}")
        return None

    def get_image_id(self, image_id, image_regexp):
        if image_id and image_regexp:
            raise ModuleError("Both image_id and image_regexp are present.")
        if image_id:
            return image_id
        elif image_regexp:
            images = self.api_helper.make_multipage_request(
                path=f'/cloud_computing/regions/{self.region_id}/images',
                query_parameters=None
            )
            for image in images:
                if re.match(image_regexp, image['name']):
                    return image['id']
            raise ModuleError(
                f'Image with regexp {image_regexp} is not found in '
                f'region {self.region_id}'
            )
        else:
            raise ModuleError('Need either image_id or image_regexp.')

    def get_flavor_id(self, flavor_id, flavor_name):
        if flavor_id and flavor_name:
            raise ModuleError("Both flavor_id and flavor_name are present.")
        if flavor_id:
            return flavor_id
        elif flavor_name:
            flavors = self.api_helper.make_multipage_request(
                path=f'/cloud_computing/regions/{self.region_id}/flavors',
                query_parameters=None
            )
            for flavor in flavors:
                if flavor['name'] == flavor_name:
                    return flavor['id']
            raise ModuleError(
                f'Flavor with name {flavor_name} is not found in '
                f'region {self.region_id}'
            )
        else:
            raise ModuleError('Need either flavor_id or flavor_name.')

    def create_instance(self):
        body = {
            'region_id': self.region_id,
            'name': self.name,
            'flavor_id': self.flavor_id,
            'image_id': self.image_id,
            'gpn_enabled': bool(self.gpn_enabled),
            'ipv6_enabled': bool(self.ipv6_enabled),
        }
        if self.ssh_key_fingerprint:
            body['ssh_key_fingerprint'] = self.ssh_key_fingerprint
        if self.backup_copies:
            body['backup_copies'] = self.backup_copies
        instance = self.api_helper.make_post_request(
            path='/cloud_computing/instances',
            body=body,
            query_parameters=None,
            good_codes=[202]
        )
        return instance

    def wait_for(self, instance):
        start_time = time.time()
        instance = self.find_instance_by_id(instance['id'])
        if not self.wait:
            return instance
        while instance['status'] != 'ACTIVE':
            time.sleep(self.update_interval)
            elapsed = time.time() - start_time
            if elapsed > self.wait:
                raise TimeOutError(
                    msg=f"Timeout while waiting instance {instance['id']}"
                    f" to become ACTIVE. Last status was {instance['status']}",
                    timeout=elapsed
                )
            instance = self.find_instance_by_id(instance['id'])
        return instance

    def run(self):
        instance = self.find_instance()
        if instance:
            instance['changed'] = NOT_CHANGED
        else:
            if not self.checkmode:
                instance = self.create_instance()
                instance = self.wait_for(instance)
            else:
                instance = {
                    'info': 'Instance shold be created, '
                            'but check_mode is activated. '
                            'no real instance was created.'
                }
            instance['changed'] = CHANGED
        return instance


class ScCloudComputingInstanceDelete(ScCloudComputingInstance):
    def __init__(
        self,
        endpoint, token,
        instance_id, region_id, name,
        wait, update_interval,
        retry_on_conflicts,
        checkmode
    ):
        self.checkmode = checkmode
        self.api_helper = ApiHelper(token, endpoint)
        self.region_id = region_id
        self.name = name
        self.instance_id = instance_id
        if update_interval > wait:
            raise ModuleError(
                f"update interval ({update_interval}) "
                f"is longer than wait ({wait})"
            )
        self.wait = wait
        self.update_interval = update_interval
        self.retry_on_conflicts = retry_on_conflicts

    def wait_for_disappearance(self, instance):
        start_time = time.time()
        instance = self.find_instance()
        while (instance):
            time.sleep(self.update_interval)
            elapsed = time.time() - start_time
            if elapsed > self.wait:
                raise TimeOutError(
                    msg=f"Timeout while waiting instance {instance['id']}"
                    f" to disappear. Last status was {instance['status']}",
                    timeout=elapsed
                )
            instance = self.find_instance()

    def retry_to_delete(self, instance):
        # pylint: disable=bad-option-value, raise-missing-from
        start_time = time.time()
        while instance:
            try:
                self.api_helper.make_delete_request(
                    path=f'/cloud_computing/instances/{instance["id"]}',
                    query_parameters=None,
                    body=None,
                    good_codes=[202]
                )

            except APIError409:
                if self.retry_on_conflicts:
                    elapsed = time.time() - start_time
                    if elapsed > self.wait:
                        raise TimeOutError(
                            msg='Timeout retrying delete for'
                                f' instance {instance["id"]}',
                            timeout=elapsed
                        )
                    time.sleep(self.update_interval)
                else:
                    raise
            instance = self.find_instance()

    def run(self):
        # pylint: disable=bad-option-value, raise-missing-from
        original_instance = self.find_instance()
        instance = original_instance
        if not instance:
            return {
                'changed': False,
                'instance_id': self.instance_id,
                'name': self.name,
                'region_id': self.region_id
            }
        if not self.checkmode:
            instance = self.retry_to_delete(instance)
            self.wait_for_disappearance(instance)
        original_instance['changed'] = CHANGED
        return original_instance