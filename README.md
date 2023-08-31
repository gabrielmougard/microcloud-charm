# Microcloud

The **Microcloud charmed operator** provides a simple way to deploy [Microcloud](https://microcloud.is/) at scale using [Juju](https://jaas.ai/).

## Roadmap

- [x] Add support for `microceph` and `microovn` in the charm
- [x] Support the setup of a microcloud cluster using `--auto`
- [ ] Support the setup of a microcloud cluster using `--preseed <YAML_CONFIG>`

## Getting started with Juju

Follow `Juju`'s [Charmed Operator Lifecycle Manager](https://juju.is/docs/olm) to boostrap your cloud of choice and create a model to host your LXD application. Once done, deploying a Microcloud unit is as simple as:

A cluster of all the cloud members:

```shell
juju deploy ch:microcloud
```

Or a cluster of 3 members with support for `microceph` and `microovn`:

```shell
juju deploy ch:microcloud --num-units 3 --config microceph=true --config microovn=true
```

## Resources

For debugging purposes, the charm allows sideloading a LXD binary (`microcloud-binary`) or a full Microcloud snap (`microcloud-snap`) by attaching resources at deploy time or later on. Both resources also accept tarballs containing architecture specific assets to support mixed architecture deployments. Those tarballs need to contain files at the root named as lxd_${ARCH} for the `microcloud-binary` resource and microcloud_${ARCH}.snap for the `microcloud-snap` resource.

```shell
juju attach-resource microcloud microcloud-snap=microcloud_21550.snap
```

To detach a resource, the operator will need to attach an empty file as Juju does not provide a mechanism to do this.

```shell
touch microcloud_empty.snap
juju attach-resource microcloud microcloud-snap=microcloud_empty.snap
```

## Tutorial

You can follow the setup instructions in `tuto/README.md` to setup a MAAS cluster with Juju with a custom image server and deploy the charm on a cluster of 3 machines.

## Additional information

- [Microcloud web site](https://microcloud.is/)
- [Microcloud GitHub](https://github.com/canonical/microcloud/)
- [Microcloud Docs](https://canonical-microcloud.readthedocs-hosted.com/en/latest/)
