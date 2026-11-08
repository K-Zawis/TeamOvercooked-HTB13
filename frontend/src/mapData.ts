/**
 * HARDCODED, ILLUSTRATIVE DATA for the map. Nothing here is computed from the logs or served by the API.
 * - LOCATIONS: one row per scan in the dataset, [log_id, area, lat, lon]. The coordinates are an area
 *   centre taken from the block code in the file name, plus a small offset so points do not overlap.
 * - SUMMARY: made-up rock-type totals per log, [log_id, lithology, total_thickness_ft, bed_count].
 * Replace both with real data (e.g. from an endpoint) when it exists.
 */
export interface MapLocation {
  log_id: string;
  name: string;
  lat: number;
  lon: number;
}

export interface MapSummary {
  log_id: string;
  lithology: string;
  total_thickness_ft: number;
  bed_count: number;
}

export const ROCKS = ["Shale", "Siltstone", "Sandstone", "Limestone"] as const;

const L: Array<[string, string, number, number]> = [
 [
  "0000000009_d-582_0000020435-0001_log01_fe_1in_md",
  "Green Canyon",
  27.611,
  -90.882
 ],
 [
  "0000000009_d-582_0000020435-0001_log02_fe_5in_md",
  "Green Canyon",
  27.469,
  -90.701
 ],
 [
  "0000000009_d-582_0000020435-0001_log03_chromatograph_ratio",
  "Green Canyon",
  27.539,
  -91.033
 ],
 [
  "0000000009_d-582_0000020435-0001_log04_fe_5in_md",
  "Green Canyon",
  27.194,
  -90.833
 ],
 [
  "0000000009_d-582_0000020435-0001_log05_chromatograph_ratio",
  "Green Canyon",
  27.239,
  -91.356
 ],
 [
  "0000000009_d-582_0000020435-0001_log06_fe_5in_md",
  "Green Canyon",
  27.66,
  -91.366
 ],
 [
  "0000000009_d-582_0000020435-0001_log07_chromatograph_ratio",
  "Green Canyon",
  27.375,
  -91.401
 ],
 [
  "0000000009_d-582_0000020435-0001_log08_fe_5in_md",
  "Green Canyon",
  27.66,
  -91.246
 ],
 [
  "0000000009_d-582_0000020435-0001_log09_chromatograph_ratio",
  "Green Canyon",
  27.684,
  -91.499
 ],
 [
  "60807402860000_gb506#2_md_mudlog",
  "Garden Banks",
  27.859,
  -92.52
 ],
 [
  "60808400140000_hadrian 2_masterlog_2in_10000-21962",
  "Keathley Canyon",
  26.327,
  -92.07
 ],
 [
  "60811404100000_gc743_dc141_st00bp00_mudlog_1in_md_9209_17052-rev",
  "Green Canyon",
  27.748,
  -91.353
 ],
 [
  "60811405120000_1inch tvd mudlog",
  "Green Canyon",
  27.132,
  -91.143
 ],
 [
  "60811405350100_shenzi-sb203-st00bp01_mudlog_4320-27400",
  "Green Canyon",
  27.331,
  -91.165
 ],
 [
  "60811405800000_608114058000_g16786_001_00_00_md5_mud_show_2",
  "Green Canyon",
  27.195,
  -91.406
 ],
 [
  "60811405800100_mdn-1_st00bp01_5 inch tvdmudlog_4560ft-22998ft",
  "Green Canyon",
  27.794,
  -91.04
 ],
 [
  "60811405800300_mdn-1_st01bp01_5 inch tvdmudlog_4560ft to 20251ft",
  "Green Canyon",
  27.805,
  -91.332
 ],
 [
  "60811405800500_608114058005_g16786_001_03_00_tvd5_mud",
  "Green Canyon",
  27.441,
  -91.316
 ],
 [
  "60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md",
  "Green Canyon",
  27.928,
  -91.042
 ],
 [
  "60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md(1)",
  "Green Canyon",
  27.182,
  -91.111
 ],
 [
  "60811406050100_ardennes_ocs-g31765 001 st00 bp01__master log_5640-36552_5 in_md",
  "Green Canyon",
  27.13,
  -91.205
 ],
 [
  "60811407300100_bp_gc743_15607_dc222_st00bp01_mudlog_1md_10700-22872",
  "Green Canyon",
  27.675,
  -90.852
 ],
 [
  "60811407570000_bp_gc743_15607_dc802_st00bp00_mudlog_5md_6900-17643",
  "Green Canyon",
  27.743,
  -90.828
 ],
 [
  "608114075800 - Allegheny Attic GC297 #3 OCS-G 08876 003 ST00 BP00  Mud Log 3346ft-13925ft MD",
  "Green Canyon",
  27.542,
  -91.224
 ],
 [
  "60812400480000_ocs-g 26419 001 st00bp00_masterlog_7860-27400_5in_md_2",
  "Walker Ridge",
  26.163,
  -90.87
 ],
 [
  "60812400810000_ocs-g 16987 003 st00bp00_1inmd_mudlog_8921_15721",
  "Walker Ridge",
  26.47,
  -90.571
 ],
 [
  "60812401020000_xom_ocs-g_20351_ju103_st00bp00_mudlog_10200-30900ft_5in_tvd",
  "Walker Ridge",
  26.694,
  -91.02
 ],
 [
  "60817410140000_mc822#10 1-1200 mud log",
  "Mississippi Canyon",
  28.673,
  -88.851
 ],
 [
  "60817410140100_mc822#10bp01 1-240 tvd mud log",
  "Mississippi Canyon",
  28.175,
  -89.306
 ],
 [
  "60817411400100_mc761_1_st1_mudlog_1md_17133_22886",
  "Mississippi Canyon",
  28.188,
  -89.026
 ],
 [
  "60817411400100_mc761_1_st1_mudlog_1tvd_17129_20968",
  "Mississippi Canyon",
  28.324,
  -89.418
 ],
 [
  "60817411400200_mc761_1_st1_bp01_mudlog_1md_17133_25775",
  "Mississippi Canyon",
  28.963,
  -89.449
 ],
 [
  "60817411420000_mc_540_#1_st00_bp00_masterlog_3400-24195_5in_md",
  "Mississippi Canyon",
  28.486,
  -89.418
 ],
 [
  "60817411700000_mc776-5_5in_md_combo_mud_log_20415_mms",
  "Mississippi Canyon",
  29.012,
  -88.713
 ],
 [
  "60817412840000_mc_776_6_ml_mudlog_5757_23721_5in_md_final",
  "Mississippi Canyon",
  28.686,
  -88.943
 ],
 [
  "60818400600100_608184006001_g08037_002_01_00_5 inch mudlog_13452ft-19912ft",
  "Atwater Valley",
  26.651,
  -89.953
 ],
 [
  "60818400650000_at0574_4_neptune_1inch_tvdmudlog_6300-17378",
  "Atwater Valley",
  26.988,
  -89.661
 ],
 [
  "Callaway 3-33 mudlog to TD.PDF",
  "Onshore US",
  38.372,
  -103.914
 ],
 [
  "Critter Creek 201-1807H_Mud_PDF_Vertical.pdf",
  "Onshore US",
  38.436,
  -104.007
 ],
 [
  "Critter Creek 202-1807H_Mud_Vertical_PDF.pdf",
  "Onshore US",
  37.814,
  -103.746
 ],
 [
  "MC765_P011_ST00BP00_1inchMD_Depth_MudLog_3686-20707ft",
  "Mississippi Canyon",
  28.854,
  -89.229
 ],
 [
  "MC765_P011_ST00BP00_5inchMD_Depth_MudLog_3686-20707ft",
  "Mississippi Canyon",
  28.577,
  -88.87
 ],
 [
  "MC765_P011_ST00BP00_5inchTVD_Depth_MudLog_3686-16495ft",
  "Mississippi Canyon",
  28.63,
  -89.147
 ],
 [
  "MC765_P011_ST00BP01_5inchMD_Depth_MudLog_18230-21150ft",
  "Mississippi Canyon",
  28.682,
  -89.417
 ],
 [
  "Mudlog-Trail Mtn1Palisades",
  "Onshore US",
  37.563,
  -103.749
 ],
 [
  "Mudlog_MD_FREEMAN 3 24 ST",
  "Onshore US",
  38.371,
  -104.103
 ],
 [
  "Rio_Lavaca_1748_Unit_1_241_MUD",
  "South Texas",
  27.909,
  -97.997
 ],
 [
  "Rio_Nueces_3744_Unit_1_212_MUD_5IN",
  "South Texas",
  27.978,
  -98.319
 ],
 [
  "Rio_Nueces_3744_Unit_1_222_5IN_MUD",
  "South Texas",
  28.581,
  -98.688
 ],
 [
  "Rio_San_Gabriel_2829_Unit_2_272_MUD",
  "South Texas",
  28.5,
  -97.869
 ],
 [
  "Shell_08852_KE001_ST01BP00_1inTVD_MudLog_17564-20124ft",
  "Keathley Canyon",
  26.731,
  -91.893
 ],
 [
  "Shell_AC0336_OCS-G-36102_001_ST00BP01_5inchMD_MudLog_9100 ft -27770 ft",
  "Alaminos Canyon",
  26.1,
  -94.577
 ]
];

const S: Array<[string, string, number, number]> = [["0000000009_d-582_0000020435-0001_log01_fe_1in_md", "Shale", 153, 11], ["0000000009_d-582_0000020435-0001_log01_fe_1in_md", "Siltstone", 69, 5], ["0000000009_d-582_0000020435-0001_log01_fe_1in_md", "Sandstone", 81, 6], ["0000000009_d-582_0000020435-0001_log01_fe_1in_md", "Limestone", 52, 4], ["0000000009_d-582_0000020435-0001_log02_fe_5in_md", "Shale", 399, 28], ["0000000009_d-582_0000020435-0001_log02_fe_5in_md", "Siltstone", 190, 14], ["0000000009_d-582_0000020435-0001_log02_fe_5in_md", "Sandstone", 104, 7], ["0000000009_d-582_0000020435-0001_log02_fe_5in_md", "Limestone", 76, 5], ["0000000009_d-582_0000020435-0001_log03_chromatograph_ratio", "Shale", 676, 48], ["0000000009_d-582_0000020435-0001_log03_chromatograph_ratio", "Siltstone", 295, 21], ["0000000009_d-582_0000020435-0001_log03_chromatograph_ratio", "Sandstone", 378, 27], ["0000000009_d-582_0000020435-0001_log03_chromatograph_ratio", "Limestone", 407, 29], ["0000000009_d-582_0000020435-0001_log04_fe_5in_md", "Shale", 1003, 72], ["0000000009_d-582_0000020435-0001_log04_fe_5in_md", "Siltstone", 565, 40], ["0000000009_d-582_0000020435-0001_log04_fe_5in_md", "Sandstone", 386, 28], ["0000000009_d-582_0000020435-0001_log04_fe_5in_md", "Limestone", 1, 1], ["0000000009_d-582_0000020435-0001_log05_chromatograph_ratio", "Shale", 662, 47], ["0000000009_d-582_0000020435-0001_log05_chromatograph_ratio", "Siltstone", 257, 18], ["0000000009_d-582_0000020435-0001_log05_chromatograph_ratio", "Sandstone", 192, 14], ["0000000009_d-582_0000020435-0001_log05_chromatograph_ratio", "Limestone", 214, 15], ["0000000009_d-582_0000020435-0001_log06_fe_5in_md", "Shale", 387, 28], ["0000000009_d-582_0000020435-0001_log06_fe_5in_md", "Siltstone", 135, 10], ["0000000009_d-582_0000020435-0001_log06_fe_5in_md", "Sandstone", 227, 16], ["0000000009_d-582_0000020435-0001_log06_fe_5in_md", "Limestone", 119, 8], ["0000000009_d-582_0000020435-0001_log07_chromatograph_ratio", "Shale", 141, 10], ["0000000009_d-582_0000020435-0001_log07_chromatograph_ratio", "Siltstone", 45, 3], ["0000000009_d-582_0000020435-0001_log07_chromatograph_ratio", "Sandstone", 72, 5], ["0000000009_d-582_0000020435-0001_log07_chromatograph_ratio", "Limestone", 49, 4], ["0000000009_d-582_0000020435-0001_log08_fe_5in_md", "Shale", 531, 38], ["0000000009_d-582_0000020435-0001_log08_fe_5in_md", "Siltstone", 301, 22], ["0000000009_d-582_0000020435-0001_log08_fe_5in_md", "Sandstone", 297, 21], ["0000000009_d-582_0000020435-0001_log08_fe_5in_md", "Limestone", 85, 6], ["0000000009_d-582_0000020435-0001_log09_chromatograph_ratio", "Shale", 172, 12], ["0000000009_d-582_0000020435-0001_log09_chromatograph_ratio", "Siltstone", 30, 2], ["0000000009_d-582_0000020435-0001_log09_chromatograph_ratio", "Sandstone", 83, 6], ["0000000009_d-582_0000020435-0001_log09_chromatograph_ratio", "Limestone", 49, 4], ["60807402860000_gb506#2_md_mudlog", "Shale", 1386, 99], ["60807402860000_gb506#2_md_mudlog", "Siltstone", 297, 21], ["60807402860000_gb506#2_md_mudlog", "Sandstone", 327, 23], ["60807402860000_gb506#2_md_mudlog", "Limestone", 106, 8], ["60808400140000_hadrian 2_masterlog_2in_10000-21962", "Shale", 697, 50], ["60808400140000_hadrian 2_masterlog_2in_10000-21962", "Siltstone", 291, 21], ["60808400140000_hadrian 2_masterlog_2in_10000-21962", "Sandstone", 224, 16], ["60808400140000_hadrian 2_masterlog_2in_10000-21962", "Limestone", 85, 6], ["60811404100000_gc743_dc141_st00bp00_mudlog_1in_md_9209_17052-rev", "Shale", 1031, 74], ["60811404100000_gc743_dc141_st00bp00_mudlog_1in_md_9209_17052-rev", "Siltstone", 432, 31], ["60811404100000_gc743_dc141_st00bp00_mudlog_1in_md_9209_17052-rev", "Sandstone", 619, 44], ["60811404100000_gc743_dc141_st00bp00_mudlog_1in_md_9209_17052-rev", "Limestone", 14, 1], ["60811405120000_1inch tvd mudlog", "Shale", 592, 42], ["60811405120000_1inch tvd mudlog", "Siltstone", 291, 21], ["60811405120000_1inch tvd mudlog", "Sandstone", 227, 16], ["60811405120000_1inch tvd mudlog", "Limestone", 226, 16], ["60811405350100_shenzi-sb203-st00bp01_mudlog_4320-27400", "Shale", 1047, 75], ["60811405350100_shenzi-sb203-st00bp01_mudlog_4320-27400", "Siltstone", 331, 24], ["60811405350100_shenzi-sb203-st00bp01_mudlog_4320-27400", "Sandstone", 407, 29], ["60811405350100_shenzi-sb203-st00bp01_mudlog_4320-27400", "Limestone", 409, 29], ["60811405800000_608114058000_g16786_001_00_00_md5_mud_show_2", "Shale", 831, 59], ["60811405800000_608114058000_g16786_001_00_00_md5_mud_show_2", "Siltstone", 251, 18], ["60811405800000_608114058000_g16786_001_00_00_md5_mud_show_2", "Sandstone", 281, 20], ["60811405800000_608114058000_g16786_001_00_00_md5_mud_show_2", "Limestone", 66, 5], ["60811405800100_mdn-1_st00bp01_5 inch tvdmudlog_4560ft-22998ft", "Shale", 607, 43], ["60811405800100_mdn-1_st00bp01_5 inch tvdmudlog_4560ft-22998ft", "Siltstone", 154, 11], ["60811405800100_mdn-1_st00bp01_5 inch tvdmudlog_4560ft-22998ft", "Sandstone", 468, 33], ["60811405800100_mdn-1_st00bp01_5 inch tvdmudlog_4560ft-22998ft", "Limestone", 9, 1], ["60811405800300_mdn-1_st01bp01_5 inch tvdmudlog_4560ft to 20251ft", "Shale", 222, 16], ["60811405800300_mdn-1_st01bp01_5 inch tvdmudlog_4560ft to 20251ft", "Siltstone", 104, 7], ["60811405800300_mdn-1_st01bp01_5 inch tvdmudlog_4560ft to 20251ft", "Sandstone", 110, 8], ["60811405800300_mdn-1_st01bp01_5 inch tvdmudlog_4560ft to 20251ft", "Limestone", 67, 5], ["60811405800500_608114058005_g16786_001_03_00_tvd5_mud", "Shale", 956, 68], ["60811405800500_608114058005_g16786_001_03_00_tvd5_mud", "Siltstone", 249, 18], ["60811405800500_608114058005_g16786_001_03_00_tvd5_mud", "Sandstone", 369, 26], ["60811405800500_608114058005_g16786_001_03_00_tvd5_mud", "Limestone", 345, 25], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md", "Shale", 510, 36], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md", "Siltstone", 239, 17], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md", "Sandstone", 251, 18], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md", "Limestone", 67, 5], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md(1)", "Shale", 470, 34], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md(1)", "Siltstone", 192, 14], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md(1)", "Sandstone", 202, 14], ["60811406050000_608114060500_g31765 001 st00 bp00__master log_5640-28740_5 in_md(1)", "Limestone", 79, 6], ["60811406050100_ardennes_ocs-g31765 001 st00 bp01__master log_5640-36552_5 in_md", "Shale", 852, 61], ["60811406050100_ardennes_ocs-g31765 001 st00 bp01__master log_5640-36552_5 in_md", "Siltstone", 367, 26], ["60811406050100_ardennes_ocs-g31765 001 st00 bp01__master log_5640-36552_5 in_md", "Sandstone", 303, 22], ["60811406050100_ardennes_ocs-g31765 001 st00 bp01__master log_5640-36552_5 in_md", "Limestone", 250, 18], ["60811407300100_bp_gc743_15607_dc222_st00bp01_mudlog_1md_10700-22872", "Shale", 636, 45], ["60811407300100_bp_gc743_15607_dc222_st00bp01_mudlog_1md_10700-22872", "Siltstone", 186, 13], ["60811407300100_bp_gc743_15607_dc222_st00bp01_mudlog_1md_10700-22872", "Sandstone", 333, 24], ["60811407300100_bp_gc743_15607_dc222_st00bp01_mudlog_1md_10700-22872", "Limestone", 68, 5], ["60811407570000_bp_gc743_15607_dc802_st00bp00_mudlog_5md_6900-17643", "Shale", 124, 9], ["60811407570000_bp_gc743_15607_dc802_st00bp00_mudlog_5md_6900-17643", "Siltstone", 77, 6], ["60811407570000_bp_gc743_15607_dc802_st00bp00_mudlog_5md_6900-17643", "Sandstone", 84, 6], ["60811407570000_bp_gc743_15607_dc802_st00bp00_mudlog_5md_6900-17643", "Limestone", 37, 3], ["608114075800 - Allegheny Attic GC297 #3 OCS-G 08876 003 ST00 BP00  Mud Log 3346ft-13925ft MD", "Shale", 824, 59], ["608114075800 - Allegheny Attic GC297 #3 OCS-G 08876 003 ST00 BP00  Mud Log 3346ft-13925ft MD", "Siltstone", 278, 20], ["608114075800 - Allegheny Attic GC297 #3 OCS-G 08876 003 ST00 BP00  Mud Log 3346ft-13925ft MD", "Sandstone", 500, 36], ["608114075800 - Allegheny Attic GC297 #3 OCS-G 08876 003 ST00 BP00  Mud Log 3346ft-13925ft MD", "Limestone", 213, 15], ["60812400480000_ocs-g 26419 001 st00bp00_masterlog_7860-27400_5in_md_2", "Shale", 924, 66], ["60812400480000_ocs-g 26419 001 st00bp00_masterlog_7860-27400_5in_md_2", "Siltstone", 397, 28], ["60812400480000_ocs-g 26419 001 st00bp00_masterlog_7860-27400_5in_md_2", "Sandstone", 332, 24], ["60812400480000_ocs-g 26419 001 st00bp00_masterlog_7860-27400_5in_md_2", "Limestone", 299, 21], ["60812400810000_ocs-g 16987 003 st00bp00_1inmd_mudlog_8921_15721", "Shale", 1077, 77], ["60812400810000_ocs-g 16987 003 st00bp00_1inmd_mudlog_8921_15721", "Siltstone", 326, 23], ["60812400810000_ocs-g 16987 003 st00bp00_1inmd_mudlog_8921_15721", "Sandstone", 426, 30], ["60812400810000_ocs-g 16987 003 st00bp00_1inmd_mudlog_8921_15721", "Limestone", 61, 4], ["60812401020000_xom_ocs-g_20351_ju103_st00bp00_mudlog_10200-30900ft_5in_tvd", "Shale", 876, 63], ["60812401020000_xom_ocs-g_20351_ju103_st00bp00_mudlog_10200-30900ft_5in_tvd", "Siltstone", 359, 26], ["60812401020000_xom_ocs-g_20351_ju103_st00bp00_mudlog_10200-30900ft_5in_tvd", "Sandstone", 488, 35], ["60812401020000_xom_ocs-g_20351_ju103_st00bp00_mudlog_10200-30900ft_5in_tvd", "Limestone", 59, 4], ["60817410140000_mc822#10 1-1200 mud log", "Shale", 683, 49], ["60817410140000_mc822#10 1-1200 mud log", "Siltstone", 310, 22], ["60817410140000_mc822#10 1-1200 mud log", "Sandstone", 471, 34], ["60817410140000_mc822#10 1-1200 mud log", "Limestone", 204, 15], ["60817410140100_mc822#10bp01 1-240 tvd mud log", "Shale", 703, 50], ["60817410140100_mc822#10bp01 1-240 tvd mud log", "Siltstone", 321, 23], ["60817410140100_mc822#10bp01 1-240 tvd mud log", "Sandstone", 252, 18], ["60817410140100_mc822#10bp01 1-240 tvd mud log", "Limestone", 223, 16], ["60817411400100_mc761_1_st1_mudlog_1md_17133_22886", "Shale", 353, 25], ["60817411400100_mc761_1_st1_mudlog_1md_17133_22886", "Siltstone", 104, 7], ["60817411400100_mc761_1_st1_mudlog_1md_17133_22886", "Sandstone", 128, 9], ["60817411400100_mc761_1_st1_mudlog_1md_17133_22886", "Limestone", 61, 4], ["60817411400100_mc761_1_st1_mudlog_1tvd_17129_20968", "Shale", 342, 24], ["60817411400100_mc761_1_st1_mudlog_1tvd_17129_20968", "Siltstone", 122, 9], ["60817411400100_mc761_1_st1_mudlog_1tvd_17129_20968", "Sandstone", 197, 14], ["60817411400100_mc761_1_st1_mudlog_1tvd_17129_20968", "Limestone", 92, 7], ["60817411400200_mc761_1_st1_bp01_mudlog_1md_17133_25775", "Shale", 1004, 72], ["60817411400200_mc761_1_st1_bp01_mudlog_1md_17133_25775", "Siltstone", 338, 24], ["60817411400200_mc761_1_st1_bp01_mudlog_1md_17133_25775", "Sandstone", 482, 34], ["60817411400200_mc761_1_st1_bp01_mudlog_1md_17133_25775", "Limestone", 55, 4], ["60817411420000_mc_540_#1_st00_bp00_masterlog_3400-24195_5in_md", "Shale", 753, 54], ["60817411420000_mc_540_#1_st00_bp00_masterlog_3400-24195_5in_md", "Siltstone", 178, 13], ["60817411420000_mc_540_#1_st00_bp00_masterlog_3400-24195_5in_md", "Sandstone", 368, 26], ["60817411420000_mc_540_#1_st00_bp00_masterlog_3400-24195_5in_md", "Limestone", 106, 8], ["60817411700000_mc776-5_5in_md_combo_mud_log_20415_mms", "Shale", 247, 18], ["60817411700000_mc776-5_5in_md_combo_mud_log_20415_mms", "Siltstone", 129, 9], ["60817411700000_mc776-5_5in_md_combo_mud_log_20415_mms", "Sandstone", 151, 11], ["60817411700000_mc776-5_5in_md_combo_mud_log_20415_mms", "Limestone", 43, 3], ["60817412840000_mc_776_6_ml_mudlog_5757_23721_5in_md_final", "Shale", 1016, 73], ["60817412840000_mc_776_6_ml_mudlog_5757_23721_5in_md_final", "Siltstone", 424, 30], ["60817412840000_mc_776_6_ml_mudlog_5757_23721_5in_md_final", "Sandstone", 436, 31], ["60817412840000_mc_776_6_ml_mudlog_5757_23721_5in_md_final", "Limestone", 121, 9], ["60818400600100_608184006001_g08037_002_01_00_5 inch mudlog_13452ft-19912ft", "Shale", 1014, 72], ["60818400600100_608184006001_g08037_002_01_00_5 inch mudlog_13452ft-19912ft", "Siltstone", 415, 30], ["60818400600100_608184006001_g08037_002_01_00_5 inch mudlog_13452ft-19912ft", "Sandstone", 292, 21], ["60818400600100_608184006001_g08037_002_01_00_5 inch mudlog_13452ft-19912ft", "Limestone", 272, 19], ["60818400650000_at0574_4_neptune_1inch_tvdmudlog_6300-17378", "Shale", 840, 60], ["60818400650000_at0574_4_neptune_1inch_tvdmudlog_6300-17378", "Siltstone", 328, 23], ["60818400650000_at0574_4_neptune_1inch_tvdmudlog_6300-17378", "Sandstone", 784, 56], ["60818400650000_at0574_4_neptune_1inch_tvdmudlog_6300-17378", "Limestone", 174, 12], ["Callaway 3-33 mudlog to TD.PDF", "Shale", 367, 26], ["Callaway 3-33 mudlog to TD.PDF", "Siltstone", 240, 17], ["Callaway 3-33 mudlog to TD.PDF", "Sandstone", 218, 16], ["Callaway 3-33 mudlog to TD.PDF", "Limestone", 81, 6], ["Critter Creek 201-1807H_Mud_PDF_Vertical.pdf", "Shale", 543, 39], ["Critter Creek 201-1807H_Mud_PDF_Vertical.pdf", "Siltstone", 241, 17], ["Critter Creek 201-1807H_Mud_PDF_Vertical.pdf", "Sandstone", 184, 13], ["Critter Creek 201-1807H_Mud_PDF_Vertical.pdf", "Limestone", 175, 12], ["Critter Creek 202-1807H_Mud_Vertical_PDF.pdf", "Shale", 1214, 87], ["Critter Creek 202-1807H_Mud_Vertical_PDF.pdf", "Siltstone", 226, 16], ["Critter Creek 202-1807H_Mud_Vertical_PDF.pdf", "Sandstone", 516, 37], ["Critter Creek 202-1807H_Mud_Vertical_PDF.pdf", "Limestone", 121, 9], ["MC765_P011_ST00BP00_1inchMD_Depth_MudLog_3686-20707ft", "Shale", 609, 44], ["MC765_P011_ST00BP00_1inchMD_Depth_MudLog_3686-20707ft", "Siltstone", 197, 14], ["MC765_P011_ST00BP00_1inchMD_Depth_MudLog_3686-20707ft", "Sandstone", 556, 40], ["MC765_P011_ST00BP00_1inchMD_Depth_MudLog_3686-20707ft", "Limestone", 94, 7], ["MC765_P011_ST00BP00_5inchMD_Depth_MudLog_3686-20707ft", "Shale", 430, 31], ["MC765_P011_ST00BP00_5inchMD_Depth_MudLog_3686-20707ft", "Siltstone", 189, 14], ["MC765_P011_ST00BP00_5inchMD_Depth_MudLog_3686-20707ft", "Sandstone", 116, 8], ["MC765_P011_ST00BP00_5inchMD_Depth_MudLog_3686-20707ft", "Limestone", 136, 10], ["MC765_P011_ST00BP00_5inchTVD_Depth_MudLog_3686-16495ft", "Shale", 779, 56], ["MC765_P011_ST00BP00_5inchTVD_Depth_MudLog_3686-16495ft", "Siltstone", 359, 26], ["MC765_P011_ST00BP00_5inchTVD_Depth_MudLog_3686-16495ft", "Sandstone", 433, 31], ["MC765_P011_ST00BP00_5inchTVD_Depth_MudLog_3686-16495ft", "Limestone", 145, 10], ["MC765_P011_ST00BP01_5inchMD_Depth_MudLog_18230-21150ft", "Shale", 478, 34], ["MC765_P011_ST00BP01_5inchMD_Depth_MudLog_18230-21150ft", "Siltstone", 214, 15], ["MC765_P011_ST00BP01_5inchMD_Depth_MudLog_18230-21150ft", "Sandstone", 419, 30], ["MC765_P011_ST00BP01_5inchMD_Depth_MudLog_18230-21150ft", "Limestone", 226, 16], ["Mudlog-Trail Mtn1Palisades", "Shale", 511, 36], ["Mudlog-Trail Mtn1Palisades", "Siltstone", 222, 16], ["Mudlog-Trail Mtn1Palisades", "Sandstone", 331, 24], ["Mudlog-Trail Mtn1Palisades", "Limestone", 151, 11], ["Mudlog_MD_FREEMAN 3 24 ST", "Shale", 918, 66], ["Mudlog_MD_FREEMAN 3 24 ST", "Siltstone", 501, 36], ["Mudlog_MD_FREEMAN 3 24 ST", "Sandstone", 328, 23], ["Mudlog_MD_FREEMAN 3 24 ST", "Limestone", 130, 9], ["Rio_Lavaca_1748_Unit_1_241_MUD", "Shale", 1031, 74], ["Rio_Lavaca_1748_Unit_1_241_MUD", "Siltstone", 334, 24], ["Rio_Lavaca_1748_Unit_1_241_MUD", "Sandstone", 348, 25], ["Rio_Lavaca_1748_Unit_1_241_MUD", "Limestone", 317, 23], ["Rio_Nueces_3744_Unit_1_212_MUD_5IN", "Shale", 770, 55], ["Rio_Nueces_3744_Unit_1_212_MUD_5IN", "Siltstone", 289, 21], ["Rio_Nueces_3744_Unit_1_212_MUD_5IN", "Sandstone", 303, 22], ["Rio_Nueces_3744_Unit_1_212_MUD_5IN", "Limestone", 51, 4], ["Rio_Nueces_3744_Unit_1_222_5IN_MUD", "Shale", 126, 9], ["Rio_Nueces_3744_Unit_1_222_5IN_MUD", "Siltstone", 76, 5], ["Rio_Nueces_3744_Unit_1_222_5IN_MUD", "Sandstone", 102, 7], ["Rio_Nueces_3744_Unit_1_222_5IN_MUD", "Limestone", 45, 3], ["Rio_San_Gabriel_2829_Unit_2_272_MUD", "Shale", 396, 28], ["Rio_San_Gabriel_2829_Unit_2_272_MUD", "Siltstone", 135, 10], ["Rio_San_Gabriel_2829_Unit_2_272_MUD", "Sandstone", 74, 5], ["Rio_San_Gabriel_2829_Unit_2_272_MUD", "Limestone", 111, 8], ["Shell_08852_KE001_ST01BP00_1inTVD_MudLog_17564-20124ft", "Shale", 303, 22], ["Shell_08852_KE001_ST01BP00_1inTVD_MudLog_17564-20124ft", "Siltstone", 71, 5], ["Shell_08852_KE001_ST01BP00_1inTVD_MudLog_17564-20124ft", "Sandstone", 102, 7], ["Shell_08852_KE001_ST01BP00_1inTVD_MudLog_17564-20124ft", "Limestone", 108, 8], ["Shell_AC0336_OCS-G-36102_001_ST00BP01_5inchMD_MudLog_9100 ft -27770 ft", "Shale", 527, 38], ["Shell_AC0336_OCS-G-36102_001_ST00BP01_5inchMD_MudLog_9100 ft -27770 ft", "Siltstone", 293, 21], ["Shell_AC0336_OCS-G-36102_001_ST00BP01_5inchMD_MudLog_9100 ft -27770 ft", "Sandstone", 168, 12], ["Shell_AC0336_OCS-G-36102_001_ST00BP01_5inchMD_MudLog_9100 ft -27770 ft", "Limestone", 33, 2]];

export const LOCATIONS: MapLocation[] = L.map(([log_id, name, lat, lon]) => ({ log_id, name, lat, lon }));
export const SUMMARY: MapSummary[] = S.map(([log_id, lithology, total_thickness_ft, bed_count]) => ({ log_id, lithology, total_thickness_ft, bed_count }));
