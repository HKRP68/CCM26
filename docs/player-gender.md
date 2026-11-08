Player gender is optional in the main Player Database. Existing records stay
**Not set** until an admin supplies a value. The column is added automatically
on startup for existing databases.

To fill in many players, open **Players → Bulk Gender**. Download the
**Player IDs + Gender CSV**, fill in its Gender column, save as CSV UTF-8, and
upload it. Alternatively, paste two columns from a spreadsheet or paste CSV:

```csv
Player ID,Gender
101,Male
102,Female
```

The header is optional. Accepted values are Male, Female, Non-binary and Other;
capitalisation does not matter, and M/F are accepted. Blank Gender cells leave
saved values alone. Enter `Not set` to clear a gender. Invalid rows, duplicate
IDs or unknown IDs prevent the whole import from saving, with an error shown
beside the submitted data.

Only the IDs supplied are updated. Different card versions have different IDs
and must each be listed. New versions created with the version form inherit the
base card's gender.

For individual changes, use the Gender dropdown on **Add Player** or
**Edit Player**. The Players table shows both Player ID and Gender, and its
Gender filter includes **Gender not set** to find records still to fill in.
The regular CSV, JSON and Excel player downloads include gender and respect
the Gender filter.

Bulk Upload Players and Data Change also accept an optional final Gender column:

```csv
Name,Rating,Category,Country,Bat Hand,Bowl Hand,Bowl Style,Bat Rating,Bowl Rating,Version,Is Active,Gender
Example Player,90,Batsman,India,Right,Right,Medium Pacer,90,30,Base,1,Female
```

Files with a header can put Gender anywhere. The downloaded player CSV (including
its ID column) can be pasted into either flow: columns are matched by header
name. Without a header, use the column order above. Older 11-column files still
work. Data Change leaves a player's gender unchanged when Gender is missing or
blank; `Not set` clears it. Invalid genders are reported as row errors and that
row is skipped, consistent with the existing bulk-upload behaviour.

JSON player imports accept `Gender` or `gender`; the admin JSON importer also
accepts `Sex` or `sex`. The standalone seed script reads Gender as well.
Master-player Challenge League snapshots include gender, and the existing
Data Change/Edit re-sync carries gender edits into those snapshots.
