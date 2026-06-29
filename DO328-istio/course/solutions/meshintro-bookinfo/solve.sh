#!/bin/bash

LAB_NAME="meshintro-bookinfo"
LAB_NS="meshintro-bookinfo"

oc login -u developer -p developer https://api.ocp4.example.com:6443

# 1) Configure traffic shifting to route 100% traffic to reviews v3

oc apply -f ~/course/labs/${LAB_NAME}/reviews-dr.yaml -n ${LAB_NS}

oc apply -f ~/course/labs/${LAB_NAME}/reviews-vs.yaml -n ${LAB_NS}
