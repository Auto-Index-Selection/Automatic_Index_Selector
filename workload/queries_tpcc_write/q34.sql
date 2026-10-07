-- type: write
UPDATE stock SET s_ytd = s_ytd + FLOOR(random() * 10)::int;
