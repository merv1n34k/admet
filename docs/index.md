---
layout: home

hero:
  name: ADMET
  text: Automatic Droplet Management Extended Toolkit
  tagline: Drive a Fluigent rig and camera, record every run, and analyse the results — from one project folder.
  image:
    src: /logo.png
    alt: ADMET
  actions:
    - theme: brand
      text: Get started
      link: /guide/introduction
    - theme: alt
      text: Install
      link: /guide/installation
    - theme: alt
      text: GitHub
      link: https://github.com/merv1n34k/admet

features:
  - title: admet control
    details: A desktop app that drives the flow units and camera, runs JSON protocols with operator gates, and records fluidics and video for every run.
    link: /control/overview
  - title: admet analyze
    details: A web server for the lab network. It reads projects, runs OpenCV droplet tracking and Cellpose imaging, and never touches the rig.
    link: /analyze/overview
  - title: Protocols as JSON
    details: Steps, parameters, gates and measurements in one validated file. Plans are previewed before anything moves.
    link: /protocols/format
  - title: Calibration you can check
    details: Fluid density, gravimetry, dead volume and viscosity runs calculate corrections from what was weighed and recorded.
    link: /control/calibration
---

## Control

![admet control](/admet_control.png)

## Analyze

![admet analyze](/admet_analyze.png)
