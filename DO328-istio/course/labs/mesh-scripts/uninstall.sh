#!/bin/bash

set -e

echo "Starting OpenShift Service Mesh cleanup..."

echo "=== Step 1: Remove Custom Resources and Configurations ==="

# Remove Kiali Console Plugin
echo "Removing Kiali Console Plugin..."
oc delete ossmconsole ossmconsole -n istio-system --ignore-not-found=true

# Remove Kiali instance
echo "Removing Kiali instance..."
oc delete kiali kiali -n istio-system --ignore-not-found=true

# Remove Istio CNI
echo "Removing Istio CNI..."
oc delete istiocni default -n istio-cni --ignore-not-found=true

# Remove Istio control plane
echo "Removing Istio control plane..."
oc delete istio default -n istio-system --ignore-not-found=true

# Remove Tempo instance
echo "Removing Tempo instance..."
oc delete tempostack sample -n tracing-system --ignore-not-found=true

echo "=== Step 2: Remove Cluster-wide Resources ==="

# Remove cluster roles and bindings
echo "Removing cluster roles and bindings..."
oc delete clusterrolebinding kiali-monitoring-rbac --ignore-not-found=true
oc delete clusterrolebinding kiali-istio-system --ignore-not-found=true
oc delete clusterrolebinding developer-kiali-istio-extended-permissions --ignore-not-found=true
oc delete clusterrole kiali-istio-system-oauth --ignore-not-found=true
oc delete clusterrole kiali-istio-extended-permissions --ignore-not-found=true

# Remove cluster-wide monitoring configuration
echo "Removing cluster-wide monitoring configuration..."
oc delete configmap cluster-monitoring-config -n openshift-monitoring --ignore-not-found=true

echo "=== Step 3: Remove Projects/Namespaces ==="

# Remove istio-related projects
echo "Removing istio-related projects..."
oc delete project istio-ingress --ignore-not-found=true
oc delete project istio-system --ignore-not-found=true
oc delete project istio-cni --ignore-not-found=true

# Remove tracing project
echo "Removing tracing project..."
oc delete project tracing-system --ignore-not-found=true

echo "=== Step 4: Remove Operators ==="

# Remove operator subscriptions
echo "Removing operator subscriptions..."
oc delete subscription kiali-ossm -n openshift-operators --ignore-not-found=true
oc delete subscription servicemeshoperator3 -n openshift-operators --ignore-not-found=true
oc delete subscription opentelemetry-product -n openshift-operators --ignore-not-found=true
oc delete subscription tempo-product -n openshift-operators --ignore-not-found=true
oc delete subscription cluster-observability-operator -n openshift-operators --ignore-not-found=true

# Remove cluster service versions (CSVs)
echo "Removing cluster service versions..."
oc get csv -n openshift-operators | grep -E "(kiali|servicemesh|opentelemetry|tempo|observability)" | awk '{print $1}' | xargs -I {} oc delete csv {} -n openshift-operators --ignore-not-found=true

# Remove operator groups if they were created specifically for these operators
echo "Removing operator groups..."
oc delete operatorgroup global-operators -n openshift-operators --ignore-not-found=true

# Remove Kubernetes Gateway API CRDs
oc delete crd referencegrants.gateway.networking.k8s.io --ignore-not-found=true
oc delete crd httproutes.gateway.networking.k8s.io --ignore-not-found=true
oc delete crd grpcroutes.gateway.networking.k8s.io --ignore-not-found=true
oc delete crd gateways.gateway.networking.k8s.io --ignore-not-found=true
oc delete crd gatewayclasses.gateway.networking.k8s.io --ignore-not-found=true


echo "OpenShift Service Mesh cleanup completed!"