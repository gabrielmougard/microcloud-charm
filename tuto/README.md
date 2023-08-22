# Tutorial : How to setup a MAAS cluster with Juju with a custom image server

## Local image server

If you have a flaky internet connection, you can setup a local image server to serve the images to MAAS.

```bash
cd local-maas-image-server && ./run.sh
```

You should now have a local image server (only serving Jammy/amd64 images for the sake of this tutorial) running at `0.0.0.0:5000`. When MAAS is setup, you can go to the MAAS UI, go to `Images` and add your new image server with the following URL: `http://0.0.0.0:5000/`. No need to wait for `images.maas.io` which I found quite slow..

## MAAS setup with Juju

You can follow the instructions in `setup/maas-setup.sh`

## Charm deployment

1) Pack the charm

```bash
charmcraft pack
```

2) Deploy the charm

```bash
juju deploy ./microcloud_ubuntu-22.04-amd64.charm --resource microcloud-snap=microcloud_21550.snap --resource microcloud-binary=lxd_21550
```
```