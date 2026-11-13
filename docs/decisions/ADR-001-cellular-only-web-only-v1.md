# ADR-001: v1 uses one cellular OBD-II device and a web app only

- **Status:** Accepted (2026-11-13, with the Phase 1 plan)
- **Date:** 2026-11-13

## Context

AutoCare has to get OBD-II data from a car to a backend and show insights to owners who often are not the driver. The usual hobby path, a cheap ELM327 Bluetooth dongle plus a phone app, has three problems for v1:

- Browsers can't reliably talk to Bluetooth dongles. Web Bluetooth doesn't exist on iPhone, and nothing runs in the background on any platform.
- A native app means installs, store review, background-execution limits and a mobile developer, none of which this MVP has.
- Data only flows while the driver's phone is in the car and connected, so an owner who isn't driving (family car, hired driver, small fleet) sees gaps.

A cellular device (Freematics ONE+ with an LTE module) has its own SIM, GNSS, motion sensor, flash/SD buffer and open-source ESP32 firmware. It sends data straight to the backend with no phone in the loop.

## Decision

- v1 supports exactly **one device type: Freematics ONE+ with LTE**, one device per car.
- The only user interface is a **Next.js web app (installable PWA)**. No iOS or Android app.
- No Bluetooth dongle support and no Web Bluetooth, not even as a stub.
- Alerts go out through the in-app feed, web push (VAPID) and email. WhatsApp/SMS are out of scope.

## Consequences

- **Good:** the user's whole job is "plug it in once, open a link". The owner sees the car whatever phone the driver has. One codebase for owner, fleet and demo views. No app-store work.
- **Good:** the device buffers during coverage gaps and uploads later, so data isn't lost when the phone leaves the car.
- **Cost:** each car needs a device (about USD 150 to 250 for the ONE+ with LTE, to confirm when buying) plus a data SIM. Until Phase 8 the simulator stands in for the device, so no hardware is needed for Phases 0 to 7.
- **Limitation, stated in the UI:** an urgent alert reaches the driver only if they've installed the PWA with notifications on, or the owner passes it on. There's no in-car screen or buzzer.
- **Risk:** iPhone web push only works for a PWA added to the home screen. Email is the fallback.
- **Later:** the device-adapter interface (ADR-002) lets a mobile developer add a Bluetooth path, or another tracker, without touching the logic downstream.
