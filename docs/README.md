# RPG Companion App — Russian Translation

This repository contains a **Russian translation of the existing [RPG Companion App](https://rpg-companion.app/)**.

It is **not a separate application and not a fork with new functionality**, but a collection of translated system files, resources, and helper scripts required to localize the interface and related game data into Russian.

## What is included here

- translated system files of the application into Russian;
- translated UI strings, character sheet sections, stats, system descriptions, and other resources;
- helper scripts for automating translation and updating localized files.

## Project Goal

The goal of this repository is to adapt the existing **[RPG Companion App](https://rpg-companion.app/)** for Russian-speaking users while preserving compatibility with the structure and resources of the original project.

## Important

- This repository is based on the existing **[RPG Companion App](https://rpg-companion.app/)**.
- The main purpose here is **Russian localization**, not reworking the application logic.
- The translation **may not be fully accurate** and in some places may require additional manual correction and verification in the application interface.
- The project is still in progress, with terminology and wording being gradually refined.

## How the translation was made

All translations, as well as the helper scripts written for them, were created **with the help of neural networks**.

Most of the translations were taken from **[dnd.su](https://dnd.su)** whenever suitable matches for the corresponding entities and texts could be found there.

If a required text could not be found on **[dnd.su](https://dnd.su)**, it was sent to a locally deployed LLM-based translator for Russian translation.

The following locally deployed model was used for translation automation: **[YandexGPT 5 Lite](https://huggingface.co/yandex/YandexGPT-5-Lite-8B-instruct-GGUF)**

Neural networks were used as a tool to speed up the localization process, after which the results were additionally reviewed and adapted to fit the structure of the application files.


## Status

The project is currently in progress and under active refinement.

Possible issues include:
- translation inaccuracies;
- terminology inconsistencies between different parts of the system;
- strings that still require manual verification in the application;
- places where the automatic translation should be replaced with a more natural wording.

## Repository Purpose

This repository is intended for:
- localizing the existing application into Russian;
- storing localized files;
- storing scripts used for translation and for future localization updates.

## Acknowledgements

Thanks to the authors of the original **[RPG Companion App](https://rpg-companion.app/)** for the source application that made this localization possible.

Special thanks to **[dnd.su](https://dnd.su)** for the existing Russian terminology and wording that helped with the localization.
