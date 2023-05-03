import re
import json
from pathlib import Path
from datetime import datetime
from subprocess import run, PIPE
from dataclasses import dataclass, field

import pytest
from pkg_resources import parse_version

TF_PROVIDER = '''
terraform {
  required_version = "%(tf_version)s"
  required_providers {
    harvester = {
      source  = "%(provider_source)s"
      version = "%(provider_version)s"
    }
  }
}

provider "harvester" {
    kubeconfig = "%(config_path)s"
}
'''


def remove_ansicode(ctx):
    if isinstance(ctx, bytes):
        ctx = ctx.decode()
    return re.sub(r"\x1b|\[\d+m", "", ctx)


@pytest.fixture(scope="session")
def tf_script_dir(request):
    return Path(request.config.getoption('--terraform-scripts-location'))


@pytest.fixture(scope="session")
def tf_provider_version(request):
    version = request.config.getoption('--terraform-provider-harvester')
    if not version:
        import requests
        resp = requests.get("https://registry.terraform.io/v1/providers/harvester/harvester")
        version = max(resp.json()['versions'], key=parse_version)
    return version


@pytest.fixture(scope="session")
def tf_executor(tf_script_dir):
    run(str(tf_script_dir / "terraform_install.sh"), stdout=PIPE, stderr=PIPE)
    executor = tf_script_dir / "bin/terraform"
    assert executor.is_file()

    yield executor


@pytest.fixture(scope="session")
def tf_harvester(api_client, tf_script_dir, tf_provider_version, tf_executor):
    harv = TerraformHarvester(tf_executor, tf_script_dir / datetime.now().strftime("%Hh%Mm_%m-%d"))
    kuebconfig = api_client.generate_kubeconfig()
    out, err, exc_code = harv.initial_provider(kuebconfig, tf_provider_version)
    assert not err and 0 == exc_code
    return harv


@pytest.fixture(scope="session")
def tf_resource(tf_provider_version):
    converter = Path("./terraform_test_artifacts/json2hcl")
    return BaseTerraformResource.for_version(tf_provider_version)(converter)


class TerraformHarvester:
    def __init__(self, executor, workdir):
        self.executor = executor.resolve()
        self.workdir = workdir
        self.workdir.mkdir(exist_ok=True)

    def exec_command(self, cmd, raw=False, **kws):
        rv = run(cmd, shell=True, stdout=PIPE, stderr=PIPE, cwd=self.workdir, **kws)

        if raw:
            return rv
        return remove_ansicode(rv.stdout), remove_ansicode(rv.stderr), rv.returncode

    def execute(self, cmd, raw=False, **kws):
        return self.exec_command(f"{self.executor} {cmd}", raw=raw, **kws)

    def initial_provider(self, kubeconfig, provider_version):
        kubefile = self.workdir / "kubeconfig"
        with open(kubefile, "w") as f:
            f.write(kubeconfig)

        with open(self.workdir / "provider.tf", "w") as f:
            f.write(TF_PROVIDER % dict(
                tf_version=">=0.13", config_path=kubefile.resolve(),
                provider_source="harvester/harvester", provider_version=provider_version
            ))

        return self.execute("init")

    def save_as(self, content, filename, ext=".tf"):
        filepath = self.workdir / f"{filename}{ext}"
        with open(filepath, "w") as f:
            f.write(content)

    def apply_resource(self, resource_type, resource_name):
        return self.execute(f"apply -auto-approve -target {resource_type}.{resource_name}")

    def destroy_resource(self, resource_type, resource_name):
        return self.execute(f"destroy -auto-approve -target {resource_type}.{resource_name}")


@dataclass
class ResourceContext:
    type: str
    name: str
    ctx: str
    raw: dict = field(default_factory=dict, compare=False)


class BaseTerraformResource:
    #: Be used to store sub classes of BaseTerraformResource
    #: Type: Dict[Type[BaseTerraformResource], List[Type[BaseTerraformResource]]]
    _sub_classes = dict()

    #: Be used to adjust whether the class is support to specific version
    #: Type: str
    support_to = "0.0.0"

    @classmethod
    def is_support(cls, target_version):
        return parse_version(target_version) >= parse_version(cls.support_to)

    @classmethod
    def for_version(cls, version):
        for c in sorted(cls._sub_classes.get(cls, []),
                        reverse=True, key=lambda x: parse_version(x.support_to).release):
            if c.is_support(version):
                return c
        return cls

    def __init_subclass__(cls):
        for parent in cls.__mro__:
            if issubclass(parent, BaseTerraformResource):
                cls._sub_classes.setdefault(parent, []).append(cls)

    def __init__(self, converter):
        self.executor = Path(converter).resolve()

    def convert_to_hcl(self, json_spec, raw=False):
        rv = run(f"echo {json.dumps(json_spec)!r} | {self.executor!s}",
                 shell=True, stdout=PIPE, stderr=PIPE)
        if raw:
            return rv
        if rv.stderr:
            raise TypeError(rv.stderr, rv.stdout, rv.returncode)
        out = rv.stdout.decode()
        out = re.sub(r'"resource"', "resource", out)  # resource should not quote
        out = re.sub(r"\"(.+?)\" =", r"\1 =", out)  # property should not quote
        out = re.sub(r"(.[^ ]+) = {", r"\1 {", out)  # block should not have `=`
        return out

    def make_resource(self, resource_type, resource_name, *, convert=True, **properties):
        rv = dict(resource={resource_type: {resource_name: properties}})
        if convert:
            return ResourceContext(resource_type, resource_name, self.convert_to_hcl(rv), rv)
        return rv


class TerraformResource(BaseTerraformResource):
    ''' https://github.com/harvester/terraform-provider-harvester/blob/v0.1.0/docs/resources/ '''
    support_to = "0.1.0"

    def ssh_key(self, resource_name, name, public_key, *, convert=True, **properties):
        return self.make_resource(
            "harvester_ssh_key", resource_name, name=name, public_key=public_key,
            convert=convert, **properties
        )

    def volume(self, resource_name, name, size=1, *, convert=True, **properties):
        size = size if isinstance(size, str) else f"{size}Gi"
        return self.make_resource(
            "harvester_volume", resource_name, name=name, size=size,
            convert=convert, **properties
        )

    def image_download(
        self, resource_name, name, display_name, url, *, convert=True, **properties
    ):
        return self.make_resource(
            "harvester_image", resource_name, name=name, display_name=display_name, url=url,
            source_type="download", convert=convert, **properties
        )

    def image_export_from_volume(
        self, resource_name, name, display_name, pvc_name, pvc_namespace,
        *, convert=True, **properties
    ):
        return self.make_resource(
            "harvester_image", resource_name, name=name, display_name=display_name,
            pvc_name=pvc_name, pvc_namespace=pvc_namespace, source_type="export-from-volume",
            convert=convert, **properties
        )

    def virtual_machine(self, resource_name, name, disks, nics, *, convert=True, **properties):
        disks.extend(properties.pop("disk", []))
        nics.extend(properties.pop("network_interface", []))
        return self.make_resource(
            "harvester_virtualmachine", resource_name, name=name, disk=disks,
            network_interface=nics, convert=convert, **properties
        )

    vm = virtual_machine  # alias

    def network(self, resource_name, name, vlan_id, *, convert=True, **properties):
        return self.make_resource(
            "harvester_network", resource_name, name=name, vlan_id=vlan_id,
            convert=convert, **properties
        )


class TerraformResource_060(TerraformResource):
    ''' https://github.com/harvester/terraform-provider-harvester/blob/v0.6.0/docs/resources/ '''
    support_to = "0.6.0"

    def storage_class(
        self, resource_name, name, replicas=1, stale_timeout=30, migratable="true",
        *, convert=True, **properties
    ):
        params = {
                "migratable": migratable,
                "numberOfReplicas": str(replicas),
                "staleReplicaTimeout": str(stale_timeout)
        }
        params.update(properties.pop('parameters', {}))
        return self.make_resource(
            "harvester_storageclass", resource_name, name=name, parameters=params,
            convert=convert, **properties
        )

    def cluster_network(self, resource_name, name, *, convert=True, **properties):
        return self.make_resource(
            "harvester_clusternetwork", resource_name, name=name, convert=convert, **properties
        )

    def vlanconfig(
        self, resource_name, name, cluster_network_name, nics, *, convert=True, **properties
    ):
        uplink = properties.pop('uplink', dict())
        uplink['nics'] = nics

        return self.make_resource(
            "harvester_vlanconfig", resource_name, name=name, uplink=uplink,
            cluster_network_name=cluster_network_name, convert=convert, **properties
        )

    def network(
        self, resource_name, name, vlan_id, cluster_network_name, *, convert=True, **properties
    ):
        return super().network(
            resource_name, name, vlan_id, cluster_network_name=cluster_network_name,
            convert=convert, **properties
        )


class TerraformResource_063(TerraformResource_060):
    ''' https://github.com/harvester/terraform-provider-harvester/blob/v0.6.3/docs/resources/ '''

    support_to = "0.6.3"

    def cloudinit_secret(
        self, resource_name, name, user_data="", network_data="", *, convert=True, **properties
    ):
        return self.make_resource(
            "harvester_cloudinit_secret", resource_name,
            user_data=user_data, network_data=network_data, convert=convert, **properties
        )