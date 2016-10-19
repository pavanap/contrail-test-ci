import time
import test_v1
from common.connections import ContrailConnections
from common import isolated_creds
from common import create_public_vn
from vn_test import VNFixture
from heat_test import *
from vm_test import VMFixture
from svc_template_fixture import *
from svc_instance_fixture import *
from project_test import ProjectFixture
from tcutils.util import get_random_name, retry
from fabric.context_managers import settings
from fabric.api import run
from fabric.operations import get, put
from tcutils.commands import ssh, execute_cmd, execute_cmd_out
import template as template
import env as env
import ConfigParser
import re
import copy

contrail_api_conf = '/etc/contrail/contrail-api.conf'


class BaseHeatTest(test_v1.BaseTestCase_v1):

    @classmethod
    def setUpClass(cls):
        super(BaseHeatTest, cls).setUpClass()
        cls.quantum_h = cls.connections.quantum_h
        cls.nova_h = cls.connections.nova_h
        cls.vnc_lib = cls.connections.vnc_lib
        cls.agent_inspect = cls.connections.agent_inspect
        cls.cn_inspect = cls.connections.cn_inspect
        cls.analytics_obj = cls.connections.analytics_obj
        cls.api_s_inspect = cls.connections.api_server_inspect
        cls.heat_api_version = 1
        cls.pt_based_svc = False
        if cls.inputs.admin_username:
            public_creds = cls.admin_isolated_creds
        else:
            public_creds = cls.isolated_creds
        cls.public_vn_obj = create_public_vn.PublicVn(
            public_creds,
            cls.inputs,
            ini_file=cls.ini_file,
            logger=cls.logger)
        cls.public_vn_obj.configure_control_nodes()

    # end setUpClass

    @classmethod
    def tearDownClass(cls):
        super(BaseHeatTest, cls).tearDownClass()
    # end tearDownClass

    def get_template(self, template_name):
        return copy.deepcopy(getattr(template, template_name))
    # end get_template

    def get_env(self, env_name):
        return copy.deepcopy(getattr(env, env_name))
    # end get_env

    def verify_vn(self, stack, env, stack_name):
        op = stack.stacks.get(stack_name).outputs
        time.sleep(5)
        for output in op:
            if output['output_key'] == 'net_id':
                vn_id = output['output_value']
                vn_obj = self.vnc_lib.virtual_network_read(fq_name=vn_id)
                vn_id = vn_obj.uuid
                vn_name = str(env['parameters']['name'])
                subnet = str(env['parameters']['subnet']) + '/' + str(env['parameters']['prefix'])
        vn_fix = self.useFixture(VNFixture(project_name=self.inputs.project_name, option='contrail',
                                           vn_name=vn_name, inputs=self.inputs, uuid=vn_id, empty_vn=True, connections=self.connections))
        self.logger.info('VN %s launched successfully via heat' % vn_name)
        assert vn_fix.verify_on_setup()
        return vn_fix
    # end verify_vn

    def update_stack(self, hs_obj, change_sets=[]):
        parameters = hs_obj.env['parameters']
        for change_set in change_sets:
            if parameters[change_set[0]] != change_set[1]:
                parameters[change_set[0]] = change_set[1]
            else:
                self.logger.info(
                    'No change seen in the Stack %s to update' % hs_obj.stack_name)
        hs_obj.update(parameters)
    # end update_stack

    def config_vn(self, stack_name=None, vn_name='net', transit=False):
        template = self.get_template('vn')
        env = self.get_env('vn')
        env['parameters']['name'] = get_random_name(stack_name)
        env['parameters']['transit'] = transit
        env['parameters']['subnet'], env['parameters'][
            'prefix'] = get_random_cidr(af=self.inputs.get_af()).split('/')
        if self.inputs.get_af() == 'v6':
            template = self.get_template('vn_dual')
            env['parameters']['subnet2'],env['parameters']['prefix2'] = get_random_cidr(af='v4').split('/')
        vn_hs_obj = self.config_heat_obj(stack_name, template, env)
        stack = vn_hs_obj.heat_client_obj
        vn_fix = self.verify_vn(stack, env, stack_name)
        self.logger.info(
            'VN %s launched successfully with ID %s' % (vn_fix.vn_name, vn_fix.vn_id))
        return vn_fix, vn_hs_obj
    # end config_vn

    def config_heat_obj(self, stack_name, template= None, env= None):
        if template == None:
            template = self.get_template(template_name=stack_name)
        if env == None:
            env = self.get_env(env_name=stack_name)
        return self.useFixture(HeatStackFixture(connections=self.connections,
                                                inputs=self.inputs, stack_name=stack_name, project_fq_name=self.inputs.project_fq_name, template=template, env=env))
    # end config_heat_obj

    def config_fip_pool(self, vn):
        stack_name = get_random_name('fip_pool')
        template = self.get_template('fip_pool')
        env = self.get_env('fip_pool')
        env['parameters']['floating_pool'] = get_random_name(
            env['parameters']['floating_pool'])
        env['parameters']['vn'] = vn.get_vn_fq_name()
        fip_pool_hs_obj = self.config_heat_obj(stack_name, template, env)
        return fip_pool_hs_obj

    def config_fip(self, fip_pool_fqdn, vmi):
        stack_name = get_random_name('fip')
        template = self.get_template('fip')
        env = self.get_env('fip')
        env['parameters']['floating_pool'] = fip_pool_fqdn
        env['parameters']['vmi'] = vmi
        env['parameters']['project_name'] = (
            ':').join(self.project.get_fq_name())
        fip_hs_obj = self.config_heat_obj(stack_name, template, env)
        return fip_hs_obj

    def config_intf_rt_table(self, prefix, si_fqdn, si_intf_type):
        stack_name = 'intf_rt_table'
        template = self.get_template('intf_rt_table')
        env = self.get_env('intf_rt_table')
        env['parameters']['intf_rt_table_name'] = get_random_name(
            env['parameters']['intf_rt_table_name'])
        env['parameters']['route_prefix'] = prefix
        env['parameters']['si_fqdn'] = si_fqdn
        env['parameters']['si_intf_type'] = si_intf_type
        intf_rt_table_hs_obj = self.config_heat_obj(stack_name, template, env)
        return intf_rt_table_hs_obj

    def config_vm(self, vn):
        stack_name = 'single_vm'
        template = self.get_template('single_vm')
        env = self.get_env('single_vm')
        env['parameters']['vm_name'] = get_random_name(
            env['parameters']['vm_name'])
        env['parameters']['net_id'] = vn.vn_id
        vm_hs_obj = self.config_heat_obj(stack_name, template, env)
        vm_fix = self.useFixture(VMFixture(project_name=self.inputs.project_name,
                                           vn_obj=vn.obj, vm_name=str(env['parameters']['vm_name']), connections=self.connections))
        assert vm_fix.wait_till_vm_is_up()
        return vm_hs_obj, vm_fix

    def config_vms(self, vn_list):
        stack_name = 'vms'
        template = self.get_template('vms')
        env = self.get_env('vms')
        env['parameters']['right_vm_name'] = get_random_name(env['parameters']['right_vm_name'])
        env['parameters']['left_vm_name'] = get_random_name(env['parameters']['left_vm_name'])
        env['parameters']['right_net_id'] = vn_list[1].vn_id
        env['parameters']['left_net_id'] = vn_list[0].vn_id
        if os.environ.has_key('ci_image'):
            env['parameters']['image'] = os.environ['ci_image']
        if self.inputs.availability_zone:
            env['parameters']['availability_zone'] = self.inputs.availability_zone
        env['parameters']['flavor'] = self.nova_h.get_default_image_flavor(env['parameters']['image'])
        self.nova_h.get_image(env['parameters']['image'])
        vms_hs_obj = self.config_heat_obj(stack_name, template, env)
        stack = vms_hs_obj.heat_client_obj
        vm_fix = self.verify_vms(stack, vn_list, env, stack_name)
        return vm_fix

    def get_stack_output(self, hs_obj, op_key):
        for op in hs_obj.heat_client_obj.stacks.get(hs_obj.stack_name).outputs:
            if op['output_key'] == op_key:
                return op['output_value']
                break

    def verify_vms(self, stack, vn_list, env, stack_name):
        op = stack.stacks.get(stack_name).outputs
        time.sleep(5)
        vm1_name= str(env['parameters']['left_vm_name'])
        vm2_name= str(env['parameters']['right_vm_name'])
        vm1_fix = self.useFixture(VMFixture(project_name=self.inputs.project_name,
                                            vn_obj=vn_list[0].obj, vm_name=vm1_name, connections=self.connections))
        vm2_fix = self.useFixture(VMFixture(project_name=self.inputs.project_name,
                                            vn_obj=vn_list[1].obj, vm_name=vm2_name, connections=self.connections))
        assert vm1_fix.wait_till_vm_is_up()
        assert vm2_fix.wait_till_vm_is_up()
        for output in op:
            if output['output_value'] == vm1_fix.vm_ip:
                self.logger.info(
                    'VM %s launched successfully' % vm1_fix.vm_name)
            elif output['output_value'] == vm2_fix.vm_ip:
                self.logger.info(
                     'VM %s launched successfully' % vm2_fix.vm_name)
        vms_list = [vm1_fix, vm2_fix]
        return vms_list
    # end verify_vn

    def config_svc_template(self, stack_name=None, scaling=False, mode='in-network-nat'):
        nomgmt = False
        ver = 1
        res_name = 'svc_tmpl'
        if mode == 'in-network' and self.inputs.get_af() == 'v6':
            res_name += '_nomgmt'
            nomgmt = True
        if self.pt_based_svc:
            res_name += '_pt'
            nomgmt = True
        if self.heat_api_version == 2:
            ver = 2
            res_name += '_v2'
        template = self.get_template(res_name)
        env = self.get_env(res_name)
        env['parameters']['mode'] = mode
        env['parameters']['name'] = get_random_name(stack_name)

        if not self.pt_based_svc:
            if mode == 'transparent':
                env['parameters']['image'] = 'tiny_trans_fw'
            if mode == 'in-network':
                env['parameters']['image'] = 'tiny_in_net'
                if self.inputs.get_af() == 'v6':
                    env['parameters']['image'] = 'ubuntu-in-net'
            env['parameters']['service_scaling'] = scaling
            if scaling:
                if self.heat_api_version == 2:
                    if mode != 'in-network-nat':
                        env['parameters']['left_shared'] = True
                        env['parameters']['right_shared'] = True
                    else:
                        env['parameters']['left_shared'] = True
                        env['parameters']['right_shared'] = False
                else:
                    if mode != 'in-network-nat':
                        env['parameters']['shared_ip_list'] = 'False,True,True'
                    else:
                        env['parameters']['shared_ip_list'] = 'False,True,False'

        if env['parameters'].has_key('image'):
            env['parameters']['flavor'] = self.nova_h.get_default_image_flavor(env['parameters']['image'])
            self.nova_h.get_image(env['parameters']['image'])
            self.nova_h.get_flavor(env['parameters']['flavor'])
        svc_temp_hs_obj = self.config_heat_obj(stack_name, template, env)
        st = self.verify_st(env, scaling, nomgmt, ver)
        return st
    # end config_svc_template

    def verify_st(self, env, scaling, nomgmt, ver):
        st_name = env['parameters']['name']
        svc_img_name = env['parameters'].get('image', None)
        flavor = self.nova_h.get_default_image_flavor(svc_img_name) if svc_img_name else None
        svc_type = env['parameters']['type']
        if nomgmt:
            if_list = ['left', 'right']
        else:
            if_list = ['management', 'left', 'right']
        svc_mode = env['parameters']['mode']
        svc_scaling = scaling
        st_fix = self.useFixture(SvcTemplateFixture(
            connections=self.connections, inputs=self.inputs, domain_name='default-domain',
            st_name=st_name, svc_img_name=svc_img_name, svc_type=svc_type, version=ver,
            if_list=if_list, svc_mode=svc_mode, svc_scaling=svc_scaling, flavor=flavor, ordered_interfaces=True))
        assert st_fix.verify_on_setup()
        return st_fix
    # end verify_st

    def config_pt_si(self, stack_name, st_fix, vn_list, max_inst=1):
        template = self.get_template(stack_name)
        env = self.get_env(stack_name)
        env['parameters']['service_template_fq_name'] = ':'.join(
            st_fix.st_fq_name)
        if env['parameters'].get('svm_name', None):
            env['parameters']['svm_name'] = get_random_name(stack_name)
        env['parameters']['right_net_id'] = vn_list[2].vn_fq_name
        env['parameters']['left_net_id'] = vn_list[1].vn_fq_name
        env['parameters']['mgmt_net_id'] = vn_list[0].vn_fq_name
        env['parameters'][
            'service_instance_name'] = get_random_name('svc_inst')
        pt_si_hs_obj = self.config_heat_obj(stack_name, template, env)
        return pt_si_hs_obj
    # end config_pt_si

    def config_pt_svm(self, stack_name, si_fqdn, vn_list, intf_rt_table_fqdn=''):
        template = self.get_template(stack_name)
        env = self.get_env(stack_name)
        env['parameters']['si_fqdn'] = si_fqdn
        env['parameters']['svm_name'] = get_random_name('svm')
        env['parameters']['right_net_id'] = vn_list[2].vn_fq_name
        env['parameters']['left_net_id'] = vn_list[1].vn_fq_name
        env['parameters']['mgmt_net_id'] = vn_list[0].vn_fq_name
        env['parameters']['intf_rt_table_fqdn'] = intf_rt_table_fqdn
        env['parameters']['def_sg_id'] = (':').join(
            self.inputs.project_fq_name) + ':default'
        vn_obj_list = []
        for vn in vn_list:
            vn_obj_list.append(vn.obj)
        stack_name = get_random_name(stack_name)
        pt_svm_hs_obj = self.config_heat_obj(stack_name, template, env)
        pt_svm_fix = self.useFixture(VMFixture(project_name=self.inputs.project_name,
                                               vn_objs=vn_obj_list, vm_name=str(env['parameters']['svm_name']), connections=self.connections))
        pt_svm_fix.wait_for_ssh_on_vm()
        return pt_svm_hs_obj, pt_svm_fix
    # end config_pt_svm

    def config_svc_instance(self, stack_name, st_fix, vn_list, max_inst=1):
        res_name = 'svc_inst'
        if st_fix.if_list == ['left', 'right']:
            res_name += '_nomgmt'
        if self.pt_based_svc:
            res_name += '_pt'
        if self.heat_api_version == 2:
            if self.inputs.get_af() == 'v6':
                res_name += '_dual'
            res_name += '_v2'
        template = self.get_template(res_name)
        env = self.get_env(res_name)
        env['parameters']['service_template_fq_name'] = ':'.join(st_fix.st_fq_name)
        env['parameters']['service_instance_name'] = get_random_name(stack_name)
        if env['parameters'].get('svm_name', None):
            env['parameters']['svm_name'] = get_random_name(stack_name)
        if st_fix.svc_mode != 'transparent':
            env['parameters']['right_net_id'] = vn_list[1].vn_fq_name
            env['parameters']['left_net_id'] = vn_list[0].vn_fq_name
        else:
            env['parameters']['right_net_id'] = 'auto'
            env['parameters']['left_net_id'] = 'auto'
        env['parameters'][
            'service_instance_name'] = get_random_name('svc_inst')
        if not self.pt_based_svc:
            env['parameters']['max_instances'] = max_inst
        if env['parameters'].has_key('image'):
            env['parameters']['flavor'] = self.nova_h.get_default_image_flavor(env['parameters']['image'])
            self.nova_h.get_image(env['parameters']['image'])
            self.nova_h.get_flavor(env['parameters']['flavor'])
            if self.inputs.availability_zone:
                env['parameters']['availability_zone'] = self.inputs.availability_zone
        si_hs_obj = self.config_heat_obj(stack_name, template, env)
        si_name = env['parameters']['service_instance_name']
        si_fix = self.verify_si(si_hs_obj.heat_client_obj, stack_name, si_name, st_fix, max_inst, st_fix.svc_mode, st_fix.image_name)
        return si_fix, si_hs_obj

        # end config_svc_instance

    @retry(delay=2, tries=5)
    def verify_svm_count(self, hs_obj, stack_name, svm_count):
        result = True
        stack = hs_obj.heat_client_obj
        op = stack.stacks.get(stack_name).outputs
        for output in op:
            if output['output_key'] == u'num_active_service_instance_vms':
                if int(output['output_value']) != int(svm_count):
                    self.logger.error('SVM Count mismatch')
                    result = False
                else:
                    self.logger.info(
                        'There are %s Active SVMs in the SI' % output['output_value'])
            if output['output_key'] == u'service_instance_vms':
                self.logger.info('%s' % output['output_value'])
        return result
    # end get_svms

    def verify_si(self, stack, stack_name, si_name, st_fix, max_inst, svc_mode, image):
        svc_inst = self.useFixture(SvcInstanceFixture(
            connections=self.connections, inputs=self.inputs,
            domain_name='default-domain', project_name=self.inputs.project_name, si_name=si_name,
            svc_template=st_fix.st_obj, if_list=st_fix.if_list))
        assert svc_inst.verify_on_setup()
        if self.pt_based_svc:
            op = stack.stacks.get(stack_name).outputs
            for output in op:
                if output['output_key'] == 'svm_id':
                    svm_id = output['output_value']
            vm = VMFixture(self.connections, uuid=svm_id)
            vm.setUp()
            vm.verify_on_setup()
            image='tiny_in_net'
            (vm.vm_username, vm.vm_password) = vm.orch.get_image_account(image)
            svc_inst._svm_list.append(vm)
        return svc_inst

    # end verify_si

    def add_route_in_svm(self, si, route):
        for subnet in route[0].vn_subnets:
            subnet = subnet['cidr']
            af = ''
            if is_v6(subnet):
                af = '-6'
            for vm in si.svm_list:
                cmd = 'ip %s route add %s dev %s' % (af, subnet, route[1])
                vm.run_cmd_on_vm([cmd], as_sudo=True)

    def config_svc_chain(self, rules, vn_list, heat_objs, stack_name='svc_chain'):
        res_name = 'svc_chain'
        if self.heat_api_version == 2:
            res_name += '_v2'
        template = self.get_template(res_name)
        env = self.get_env(res_name)
        env['parameters']['policy_name'] = get_random_name('sc')
        if self.heat_api_version == 2:
             template['resources']['policy']['properties']['network_policy_entries']['network_policy_entries_policy_rule'].extend(rules)
        else:
             env['parameters']['policy_name'] = get_random_name('sc')
             env['parameters']['src_vn_id'] = vn_list[0].uuid
             env['parameters']['dst_vn_id'] = vn_list[1].uuid
             template['resources']['private_policy']['properties']['entries']['policy_rule'].extend(rules)
        svc_hs_obj = self.config_heat_obj(stack_name, template, env)
        if self.heat_api_version != 2:
            return
        op = svc_hs_obj.heat_client_obj.stacks.get(stack_name).outputs
        for output in op:
            if output['output_key'] == 'policy_id':
                policy_id = output['output_value']
            if output['output_key'] == 'policy_fqname':
                policy_fqname = output['output_value']
        policy_fqname = ':'.join(policy_fqname)
        # Hack, policy association doesn't work through heat, rewrite after bug fix
        heat_objs[0].policys = getattr(heat_objs[0], 'policys', [])
        heat_objs[1].policys = getattr(heat_objs[1], 'policys', [])
        heat_objs[0].policys.append(policy_fqname.split(':'))
        heat_objs[1].policys.append(policy_fqname.split(':'))
        vn_list[0].bind_policies(heat_objs[0].policys, vn_list[0].uuid)
        vn_list[1].bind_policies(heat_objs[1].policys, vn_list[1].uuid)
        svc_hs_obj.addCleanup(vn_list[0].unbind_policies, vn_list[0].uuid, [policy_fqname.split(':')])
        svc_hs_obj.addCleanup(vn_list[1].unbind_policies, vn_list[1].uuid, [policy_fqname.split(':')])
        return svc_hs_obj
    # end config_svc_chain

    def config_v2_svc_chain(self, stack_name):
        svc_pt_hs = self.config_heat_obj(stack_name)
        stack = svc_pt_hs.heat_client_obj
        op = stack.stacks.get(stack_name).outputs
        time.sleep(5)
        for output in op:
            if output['output_key'] == 'left_VM_ID':
                left_vm_id = output['output_value']
            elif output['output_key'] == 'right_VM_ID': 
                right_vm_id = output['output_value']
            elif output['output_key'] == 'left_vn_FQDN': 
                left_vn_fqdn = output['output_value']
            elif output['output_key'] == 'right_vn_FQDN':
                right_vn_fqdn = output['output_value']
            elif output['output_key'] == 'si_fqdn':
                si_fqdn = output['output_value']
            elif output['output_key'] == 'si2_fqdn':
                si2_fqdn = output['output_value']
                si2_fqdn=":".join(si2_fqdn)
        #Update the policy
        si_fqdn=":".join(si_fqdn)
        left_vn_fqdn=":".join(left_vn_fqdn)
        right_vn_fqdn=":".join(right_vn_fqdn)
        if 'multi' in stack_name:
            self.update_stack(svc_pt_hs, change_sets=[['left_vn_fqdn', left_vn_fqdn], ['right_vn_fqdn', right_vn_fqdn], ['service_instance1_fq_name', si_fqdn], ['service_instance2_fq_name', si2_fqdn]])
        else:
            self.update_stack(svc_pt_hs, change_sets=[['left_vn_fqdn', left_vn_fqdn], ['right_vn_fqdn', right_vn_fqdn], ['service_instance_fq_name', si_fqdn]])
        left_vm = VMFixture(connections=self.connections,uuid = left_vm_id, image_name = 'cirros-0.3.0-x86_64-uec')
        left_vm.read()
        left_vm.verify_on_setup()
        right_vm = VMFixture(connections=self.connections,uuid = right_vm_id, image_name = 'cirros-0.3.0-x86_64-uec')
        right_vm.read()
        right_vm.verify_on_setup()
        assert left_vm.ping_with_certainty(right_vm.vm_ip, expectation=True)

    def config_svc_rule_v1(self, direction='<>', proto='icmp', src_ports=None, dst_ports=None, src_vns=None, dst_vns=None, si_fq_names=[]):
        template = self.get_template('svc_rule')
        src_ports = src_ports or [(-1,-1)]
        dst_ports = dst_ports or [(-1,-1)]
        template['direction'] = direction
        template['protocol'] = proto

        template['src_ports'] = []
        template['dst_ports'] = []
        for port_st, port_end in src_ports:
            port_dict = {}
            port_dict['start_port'] = port_st
            port_dict['end_port'] = port_end
            template['src_ports'].append(port_dict)
        for port_st, port_end in dst_ports:
            port_dict = {}
            port_dict['start_port'] = port_st
            port_dict['end_port'] = port_end
            template['dst_ports'].append(port_dict)

        template['src_addresses'] = []
        template['dst_addresses'] = []
        for vn in src_vns:
            vn_dict = {}
            vn_dict['virtual_network'] = vn.vn_fq_name
            template['src_addresses'].append(vn_dict)
        for vn in dst_vns:
            vn_dict = {}
            vn_dict['virtual_network'] = vn.vn_fq_name
            template['dst_addresses'].append(vn_dict)

        template['action_list']['apply_service'] = si_fq_names
        return template

    def config_svc_rule_v2(self, direction='<>', proto='icmp', src_ports=None, dst_ports=None, src_vns=None, dst_vns=None, si_fq_names=[]):
        template = self.get_template('svc_rule_v2')
        src_ports = src_ports or [(-1,-1)]
        dst_ports = dst_ports or [(-1,-1)]
        template['network_policy_entries_policy_rule_direction'] = direction
        template['network_policy_entries_policy_rule_protocol'] = proto

        template['network_policy_entries_policy_rule_src_ports'] = []
        template['network_policy_entries_policy_rule_dst_ports'] = []
        for port_st, port_end in src_ports:
            port_dict = {}
            port_dict['network_policy_entries_policy_rule_src_ports_start_port'] = port_st
            port_dict['network_policy_entries_policy_rule_src_ports_end_port'] = port_end
            template['network_policy_entries_policy_rule_src_ports'].append(port_dict)
        for port_st, port_end in dst_ports:
            port_dict = {}
            port_dict['network_policy_entries_policy_rule_dst_ports_start_port'] = port_st
            port_dict['network_policy_entries_policy_rule_dst_ports_end_port'] = port_end
            template['network_policy_entries_policy_rule_dst_ports'].append(port_dict)

        template['network_policy_entries_policy_rule_src_addresses'] = []
        template['network_policy_entries_policy_rule_dst_addresses'] = []
        for vn in src_vns:
            vn_dict = {}
            vn_dict['network_policy_entries_policy_rule_src_addresses_virtual_network'] = vn.vn_fq_name
            template['network_policy_entries_policy_rule_src_addresses'].append(vn_dict)
        for vn in dst_vns:
            vn_dict = {}
            vn_dict['network_policy_entries_policy_rule_dst_addresses_virtual_network'] = vn.vn_fq_name
            template['network_policy_entries_policy_rule_dst_addresses'].append(vn_dict)

        template['network_policy_entries_policy_rule_action_list']['network_policy_entries_policy_rule_action_list_apply_service'] = si_fq_names
        return template

    def config_svc_rule(self, direction='<>', proto='icmp', src_ports=None, dst_ports=None, src_vns=None, dst_vns=None, si_fq_names=[]):
        if self.heat_api_version == 2:
            return self.config_svc_rule_v2(direction, proto, src_ports, dst_ports, src_vns, dst_vns, si_fq_names)
        else:
            return self.config_svc_rule_v1(direction, proto, src_ports, dst_ports, src_vns, dst_vns, si_fq_names)
    # end config_svc_rule
