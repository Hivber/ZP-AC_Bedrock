# ZP-AC Resource Pack

## Included assets

- `textures/ui/zp_ac_logo.png`: ZP-AC brand mark.
- `textures/ui/icon_shield.png`: anti-cheat / protection icon.
- `textures/ui/icon_credit.png`: public credit icon.
- `textures/ui/icon_report.png`: player report icon.
- `textures/ui/menu_panel.png`: reusable menu panel texture.
- `pack_icon.png`: pack list icon.

## Localization

The pack declares `zh_CN` and `en_US` in `texts/languages.json`. Each language file provides `pack.name` and `pack.description`; the manifest references those keys.

## Important UI integration boundary

The behavior pack currently creates forms with `@minecraft/server-ui` (`ActionFormData` and `ModalFormData`). The Script API does not expose a supported way to assign arbitrary resource-pack PNGs as those native forms' backgrounds or button icons. Therefore, the textures are packaged reusable assets, but they do not automatically reskin the native forms.

A fully custom JSON UI requires a separately implemented UI definition and a supported client-side entry point, followed by testing against the exact Bedrock client version. Adding PNGs alone cannot provide that integration. Do not claim that the existing native forms are custom-skinned.
