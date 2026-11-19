"""Benchmark task families. Each: (train, validation, holdout). Inputs disjoint across splits."""
def C(pairs): return [{"input": i, "output": o} for i, o in pairs]
TASKS = {
 "slug": (C([("Hello World","hello-world"),("  Big   Data ","big-data")]), C([("Memetic OS Core","memetic-os-core")]), C([(" A b ","a-b")])),
 "csv_sum": (C([("1,2,3",6),("10,5",15),("2,2",4)]), C([("4,4,4,4",16)]), C([("7",7)])),
 "initials": (C([("ada lovelace","AL"),("alan mathison turing","AMT")]), C([("grace hopper","GH")]), C([("x y z","XYZ")])),
 "dedupe_sort": (C([("b,a,b,c","a,b,c"),("z,z,y","y,z")]), C([("q,p,q,p","p,q")]), C([("m","m")])),
 "word_count": (C([("the cat sat",3),("  a  b ",2)]), C([("one two three four",4)]), C([("x",1)])),
 "snake_case": (C([("Hello World","hello_world"),(" Data  Lake ","data_lake")]), C([("Memetic Core Unit","memetic_core_unit")]), C([("A B","a_b")])),
 "title_words": (C([("hello  world","Hello World"),(" big data ","Big Data")]), C([("memetic operating system","Memetic Operating System")]), C([("a","A")])),
 "csv_max_word": (C([("b,a,c","c"),("x,y","y")]), C([("m,q,p","q")]), C([("k","k")])),
 "reverse_words": (C([("one two three","three two one"),("a b","b a")]), C([("x y z w","w z y x")]), C([("solo","solo")])),
 "kebab_sorted": (C([("Zeta Alpha","alpha-zeta"),("c B a","a-b-c")]), C([("Delta Gamma Beta","beta-delta-gamma")]), C([("Q","q")])),
}
