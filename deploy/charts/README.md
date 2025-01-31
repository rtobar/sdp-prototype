
Helm Deployment
===============

Prerequisites
-------------

### Kubernetes

You will need Kubernetes installed. [Docker for
Desktop](https://www.docker.com/products/docker-desktop) includes a
workable one-node Kubernetes installation - just need to activate it
in the settings. Alternatively, you can install
[Minikube](https://kubernetes.io/docs/tasks/tools/install-minikube/).

Note that on both Windows and Mac, this will run containers within a
VM that has limited resources. You might want to increase this to at
least 3 GB. For Docker this can be found in the settings, for Minikube
you need to specify it on the command line:

    $ minikube --mem 4096 ...
    
#### Micro8ks for Kubernetes

Canonical supports **microk8s** for Ubuntu Linux distributions - and it 
is also available for many other distributions (42 according to 
[here](https://github.com/ubuntu/microk8s#accessing-kubernetes)). It gives a more-or-less 
'one line' Kubernetes installation

- To install type 'sudo snap install microk8s --classic'
- 'microk8s.start' will start the Kubernetes system
- 'microk8s.enable dns' is required for the SDP development system
- 'microk8s.status' should show that things are active
- microk8s will install 'kubectl' as 'microk8s.kubectl' - unless you have another 
Kubernetes installation in parallel you may wish to type 
'sudo snap alias microk8s.kubectl kubectl'

Initially I had problems with microk8s PODs not communicating. 'microk8s.inspect' 
correctly told me the reason was that I needed an 'sudo iptables -P FORWARD ACCEPT'
and things worked fine after this!

microk8s works differently to minikube and does not need drivers or mem= options


### Helm

Furthermore you will need to install the Helm utility. It is available
from most typical package managers, see [Using
Helm](https://helm.sh/docs/using_helm/). Note that for the moment we
are using Helm version 2, as at the time of writing version 3 is in
early alpha.

It may be available in the 'snap' installation system (eg. on
recent Ubuntu installations)

Once you have it available, you will typically need to initialise it
(this will create the "Tiller" controller):

    $ helm init

Deploying SDP
-------------

### Creating etcd-operator

We are not creating an etcd cluster ourselves, but instead leave it to
"operator" that needs to be installed first. Simply execute:

    $ helm install stable/etcd-operator -n etcd

If you now execute:

    $ kubectl get pod --watch

You should eventually see an pod called
`etcdop-etcd-operator-etcd-operator-[...]` in "Running" state (yes,
Helm is exceedingly redundant with its names). If not wait a bit, if
you try to go to the next step before this has completed there's a
chance it will fail.

### Deploy the prototype

At this point you should be able to deploy

    $ cd [sdp-prototype]/deploy/charts
    $ helm install sdp-prototype -n sdp-prototype

You can again watch the fireworks using `kubectl`:

    $ kubectl get pod --watch

Pods asocciated with Tango might go down a couple times before they
start correctly, this seems to be normal. You can check the logs of
pods (copy the full name from `kubectl` output) to verify that they
are doing okay:

    $ kubectl logs sdp-prototype-workflow-testdeploy-[...]
    INFO:main:Waiting for processing block...
    $ kubectl logs sdp-prototype-helm-[...]
    ...
    INFO:main:Found 0 existing deployments.
    $ kubectl logs sdp-protoype-sdp-master-[...]
    ...
    Ready to accept request

Just to name a few. If it's looking like this, there's a good chance
everything deployed correctly.

Testing it out
--------------

### Connecting to configuration database

The deployment scripts should have exposed the SDP configuration
database (i.e. etcd) via a NodePort service. For Docker Desktop, this
should automatically expose the port on localhost, you just need to
find out which one:

    $ kubectl get service sdp-prototype-etcd-nodeport
    NAME                          TYPE       CLUSTER-IP      EXTERNAL-IP   PORT(S)          AGE
    sdp-prototype-etcd-nodeport   NodePort   10.97.188.221   <none>        2379:32234/TCP   3h56m

Set the environment variable `SDP_CONFIG_PORT` according to the
second part of the `PORTS(S)` column:

    $ export SDP_CONFIG_PORT=32234

For Minikube, you need to set both `SDP_CONFIG_HOST` and
`SDP_CONFIG_PORT`, but you can easily query both using
`minikube service`:

    $ minikube service --url sdp-prototype-etcd-nodeport
    http://192.168.39.45:32234
    $ export SDP_CONFIG_HOST=192.168.39.45
    $ export SDP_CONFIG_PORT=32234

This will allow you to connect with the `sdpcfg` utility:

    $ pip install -U ska-sdp-config
    $ sdpcfg ls -R /
    Keys with / prefix:

Which correctly shows that the configuration is currently empty.

### Start a deployment test workflow

Assuming the configuration is prepared as explained in the previous
section, we can now add a processing block to the configuration:

    $ sdpcfg process realtime:testdeploy:0.0.7
    OK, pb_id = realtime-20190807-0000
    $ sdpcfg ls values -R /
    Keys with / prefix:
    /pb/realtime-20190807-0000 = {
      "parameters": {},
      "pb_id": "realtime-20190807-0000",
      "sbi_id": null,
      "scan_parameters": {},
      "workflow": {
        "id": "testdeploy",
        "type": "realtime",
        "version": "0.0.7"
      }
    }
    /pb/realtime-20190807-0000/owner = {
      "command": [
        "dummy_workflow.py"
      ],
      "hostname": "sdp-prototype-workflow-testdeploy-[...]",
      "pid": 6
    }

Notice that the workflow was claimed immediately by one of the
containers.

### Use it to add a deployment

The special property of the deployment test workflow is that it will
create deployments automatically depending on workflow parameters. It
even watches the processing block parameters and will add deployments
while it is running. Let's try this out:

    $ sdpcfg edit /pb/realtime-[...]

At this point an editor should open with the processing block
information formatted as YAML. Change the "`parameters: {}`" line to
read as follows:

    parameters:
      mysql:
        type: helm
        args:
          chart: stable/mysql

This will cause the workflow to deploy a new mysql instance, as we can
easily check:

    $ sdpcfg ls values -R /deploy
    Keys with /deploy prefix:
    /deploy/realtime-20190807-0000-mysql = {
      "args": {
        "chart": "stable/mysql"
      },
      "deploy_id": "realtime-20190807-0000-mysql",
      "type": "helm"
    }

This causes Helm to get called, so you should be able to check:

    $ helm list
    NAME                        	REVISION	UPDATED                 	STATUS  	CHART              	APP VERSION	NAMESPACE
    etcd                        	1       	Wed Aug  7 12:35:47 2019	DEPLOYED	etcd-operator-0.8.4	0.9.3      	default  
    realtime-20190807-0000-mysql	1       	Wed Aug  7 13:45:33 2019	DEPLOYED	mysql-1.3.0        	5.7.14     	sdp-helm 
    sdp-prototype               	1       	Wed Aug  7 13:38:42 2019	DEPLOYED	sdp-prototype-0.2.0	1.0        	default

Note the deployment associated with the processing block. Note that it
was deployed into the name space `sdp-helm`, so to view the created pod we
have to ask as follows:

    $ kubectl get pod -n sdp-helm
    NAME                                           READY   STATUS    RESTARTS   AGE
    realtime-20190807-0000-mysql-89f658f78-mfstr   1/1     Running   0          6m20s

### Cleaning up

Finally, let us remove the processing block from the configuration:

    $ sdpcfg delete /pb/realtime-[...]

If you re-run the commands from the last section you will notice that
this correctly causes all changes to the cluster configuration to be
undone as well.

Accessing Tango
---------------

By default the chart installs the iTango shell pod from the tango-base
chart. You can access it as follows:

    $ kubectl exec -it itango-tango-base-sdp-prototype /venv/bin/itango3

You should be able to query the SDP Tango devices:

    In [1]: lsdev
    Device                                   Alias                     Server                    Class
    ---------------------------------------- ------------------------- ------------------------- --------------------
    mid_sdp/elt/master                                                 SDPMaster/1               SDPMaster
    sys/tg_test/1                                                      TangoTest/test            TangoTest
    sys/database/2                                                     DataBaseds/2              DataBase
    sys/access_control/1                                               TangoAccessControl/1      TangoAccessControl
    mid_sdp/elt/subarray_1                                             SDPSubarray/1             SDPSubarray

This allows direction interaction with the devices, such as querying and
and changing attributes and issuing commands:

    In [2]: d = DeviceProxy('mid_sdp/elt/subarray_1')

    In [3]: d.obsState
    Out[3]: <obsState.IDLE: 0>
    In [4]: d.state()
    Out[4]: tango._tango.DevState.OFF
    In [5]: d.adminMode = 'ONLINE'
    
    In [6]: d.AssignResources('')
    
    In [7]: d.state()
    Out[7]: tango._tango.DevState.ON
    In [8]: d.obsState
    Out[8]: <obsState.IDLE: 0>


Troubleshooting
---------------

## etcd doesn't start (DNS problems)

Something that often happens on home set-ups is that `sdp-prototype-etcd`
doesn't start, which means that quite a bit of the
SDP system will not work. Try executing `kubectl logs` on the pod to
get a log. You might see something like this as the last three lines:

    ... I | pkg/netutil: resolving sdp-prototype-etcd-9s4hbbmmvw.k8s-sdp-prototype-etcd.default.svc:2380 to 10.1.0.21:2380
    ... I | pkg/netutil: resolving sdp-prototype-etcd-9s4hbbmmvw.k8s-sdp-prototype-etcd.default.svc:2380 to 92.242.132.24:2380
    ... C | etcdmain: failed to resolve http://sdp-prototype-etcd-9s4hbbmmvw.sdp-prototype-etcd.default.svc:2380 to match --initial-cluster=sdp-prototype-etcd-9s4hbbmmvw=http://sdp-prototype-etcd-9s4hbbmmvw.sdp-prototype-etcd.default.svc:2380 ("http://10.1.0.21:2380"(resolved from "http://sdp-prototype-etcd-9s4hbbmmvw.sdp-prototype-etcd.default.svc:2380") != "http://92.242.132.24:2380"(resolved from "http://sdp-prototype-etcd-9s4hbbmmvw.sdp-prototype-etcd.default.svc:2380"))

This informs you that `etcd` tried to resolve its own address, and for
some reason got two different answers both times. Interestingly, the 
`92.242.132.24` address is not actually in-cluster, but from the Internet,
and re-appears if we attempt to `ping` a nonexistant DNS name:

    $ ping does.not.exist
	Pinging does.not.exist [92.242.132.24] with 32 bytes of data:
    Reply from 92.242.132.24: bytes=32 time=25ms TTL=242

What is going on here is that that your ISP has installed a DNS server
that re-directs unknown DNS names to some server showing a "helpful"
error message complete with a bunch of advertisment. For some reason
this seems to cause a problem with Kubernetes' internal DNS resolution.

How can this be prevented? Theoretically it should be enough to force
the DNS server to one that doesn't have this problem (like Google's
`8.8.8.8` and `8.8.4.4` DNS servers), but that is tricky to get working.
Alternatively you can simply restart the entire thing until it works.
Unfortunately this is not quite as straightforward with `etcd-operator`,
as it sets the `restartPolicy` to `Never`, which means that any `etcd`
pod only gets once chance, and then will remain `Failed` forever. The
quickest way I have found is to delete the `EtcdCluster` object, then
`upgrade` the chart in order to re-install it:

    $ kubectl delete etcdcluster sdp-prototype-etcd
    $ helm upgrade sdp-prototype sdp-prototype

This can generally be repeated until by pure chance the two DNS resolutions
return the same result and `etcd` starts up.
